"""Test session — one test run lifecycle for a single config.

Orchestrates: recorder -> pipelines -> judgment -> result.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from scipy.io import wavfile

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.judgment import JudgmentEngine, _runtime_channel_defaults
from app.service.analysis.live_display_dispatcher import LiveDisplayDispatcher
from app.service.analysis.pipeline import AnalysisPipeline
from app.service.analysis.selected_channels import project_selected_channels
from app.service.result_dashboard_layout import (
    CHANNEL_PRIMARY_ACOUSTIC,
    PANEL_FAMILY_ACOUSTIC,
    resolve_layout_channel,
)
from model.audio_data_table import AudioDataTable

_TZ8 = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ManualLabelUpdateResult:
    success: bool
    message: str = ""
    audio_data_id: str | None = None


class TestSession:
    """Lifecycle manager for a single test run bound to one config."""

    __test__ = False

    def __init__(
        self,
        config: dict,
        bus,
        ai_service,
        mic_cal: dict,
        workorder: dict,
        audio_dir: str,
    ):
        self._config = config
        self._bus = bus
        self._ai_service = ai_service
        self._mic_cal = mic_cal
        self._workorder = workorder
        self._audio_dir = audio_dir

        self.config_id: int = config["config_id"]
        self._record_duration: float = config.get("record_duration", 5.0)
        self._analysis_duration: float = config.get("analysis_duration", 3.0)
        self._sample_rate: float = float(config.get("input_device_samplerate", 44100))

        self._pipelines: list[AnalysisPipeline] = []
        self._live_display_dispatcher: LiveDisplayDispatcher | None = None
        self._buffer: np.ndarray | None = None
        self._source_channels: tuple[int, ...] = ()
        self._real_to_logical_channel: dict[int, int] = {}
        self._judgment: JudgmentEngine = JudgmentEngine(ai_service)
        self._last_result: JudgmentResult | None = None
        self._last_loudness_trace_cache: dict[int, dict] | None = None
        self._last_ai_review: dict[str, Any] | None = None
        self._audio_file_path: str | None = None
        self._audio_data_id: str | None = None
        self._test_run_id: str | None = None
        self._finished: bool = False
        self._attach_chunk_index: int = 0
        self._attach_snapshot: Any | None = None
        self._processed_samples_since_attach: int = 0
        self._recorder_chunk_window_samples: int = 0
        self._attach_pending: bool = False
        self._pending_attach_chunks: list[tuple[np.ndarray, int]] = []
        self._on_session_done_cb: Callable[[int], Any] | None = None
        self._live_loudness_channel_index: int | None = None

        # Connection refs set by TestManager for signal disconnection
        self._chunk_conn: Callable | None = None
        self._done_lambda: Callable | None = None
        self._error_lambda: Callable | None = None
        self._monitor_lambda: Callable | None = None
        self._monitor_error_lambda: Callable | None = None

    def _setup_pipelines(self):
        """Create one AnalysisPipeline per selected channel."""
        self.stop_live_display()
        selected = self._config.get("selected_channels", [])
        self._live_loudness_channel_index = self._default_live_loudness_channel(selected)
        channels_cfg = {ch["channel_index"]: ch for ch in self._config.get("channels", [])}
        self._pipelines = []
        for ch_idx in selected:
            ch_cfg = channels_cfg.get(ch_idx)
            if ch_cfg is None:
                ch_cfg = _runtime_channel_defaults(ch_idx)
            sensitivity = self._get_sensitivity(ch_idx)
            self._pipelines.append(
                AnalysisPipeline(
                    channel_index=ch_idx,
                    channel_config=ch_cfg,
                    sample_rate=self._sample_rate,
                    mic_sensitivity=sensitivity,
                )
            )
        self._live_display_dispatcher = LiveDisplayDispatcher(
            pipelines=self._pipelines,
            bus=self._bus,
            config_id=self.config_id,
        )

    def set_live_loudness_channel_index(self, channel_index: int | None):
        if channel_index is None:
            self._live_loudness_channel_index = None
            return
        normalized_channel = self._as_non_bool_int(channel_index)
        if normalized_channel is None:
            return
        selected_channels = {
            selected
            for selected in (
                self._as_non_bool_int(channel)
                for channel in self._config.get("selected_channels", [])
            )
            if selected is not None
        }
        if normalized_channel in selected_channels:
            self._live_loudness_channel_index = normalized_channel

    def prepare_for_attach(self):
        """Buffer chunks until the recorder-owned attach boundary is known."""
        self._attach_pending = True
        self._pending_attach_chunks = []

    def attach_to_recorder(self, *, chunk_index: int, snapshot: Any | None = None):
        """Record the chunk boundary where this session becomes live."""
        self._attach_chunk_index = max(0, int(chunk_index))
        self._attach_snapshot = snapshot
        self._processed_samples_since_attach = 0
        self._recorder_chunk_window_samples = 0
        pending_chunks = self._pending_attach_chunks
        self._pending_attach_chunks = []
        self._attach_pending = False
        for pending_chunk, pending_index in pending_chunks:
            self._process_chunk(pending_chunk, pending_index)

    def _on_chunk(self, chunk: np.ndarray, chunk_index: int):
        """Process one audio chunk through all pipelines and emit results."""
        if self._attach_pending:
            self._pending_attach_chunks.append((chunk.copy(), chunk_index))
            return
        self._process_chunk(chunk, chunk_index)

    def _processed_sample_boundary_since_attach(self, *, chunk_index: int, chunk_samples: int) -> int:
        chunk_window_samples = max(0, int(chunk_samples))
        if chunk_window_samples > self._recorder_chunk_window_samples:
            self._recorder_chunk_window_samples = chunk_window_samples

        if self._recorder_chunk_window_samples <= 0:
            return 0

        relative_chunk_count = max(0, int(chunk_index) - self._attach_chunk_index + 1)
        processed_samples = relative_chunk_count * self._recorder_chunk_window_samples
        target_samples = max(0, int(self._sample_rate * self._record_duration))
        if target_samples > 0:
            return min(processed_samples, target_samples)
        return processed_samples

    def _process_chunk(self, chunk: np.ndarray, chunk_index: int):
        if chunk_index < self._attach_chunk_index:
            return

        chunk_samples = int(chunk.shape[0]) if chunk.ndim > 0 else 0
        self._processed_samples_since_attach = self._processed_sample_boundary_since_attach(
            chunk_index=chunk_index,
            chunk_samples=chunk_samples,
        )
        elapsed = (
            self._processed_samples_since_attach / self._sample_rate
            if self._sample_rate > 0
            else 0.0
        )
        frame_run_id = self._runtime_frame_run_id()
        sample_end = self._processed_samples_since_attach
        sample_start = max(0, sample_end - chunk_samples)

        self._submit_live_display_chunk(
            chunk,
            elapsed,
            frame_run_id,
            sample_start=sample_start,
            sample_end=sample_end,
        )

        self._bus.recording_progress.emit(self.config_id, elapsed)

        if not self._finished and elapsed >= self._record_duration:
            self._finished = True
            if self._on_session_done_cb is not None:
                self._on_session_done_cb(self.config_id)

    def _submit_live_display_chunk(
        self,
        chunk: np.ndarray,
        elapsed: float,
        frame_run_id: str,
        *,
        sample_start: int | None = None,
        sample_end: int | None = None,
    ) -> None:
        dispatcher = self._live_display_dispatcher
        if dispatcher is None:
            return
        try:
            submit_kwargs = {
                "elapsed_time": elapsed,
                "frame_run_id": frame_run_id,
                "live_loudness_channel_index": self._live_loudness_channel_index,
            }
            submit = dispatcher.submit
            if self._live_display_submit_accepts_sample_span(submit):
                submit_kwargs["sample_start"] = sample_start
                submit_kwargs["sample_end"] = sample_end
            submit(chunk, **submit_kwargs)
        except Exception as exc:
            self._bus.log.emit(
                f"[TestSession] WARNING: live display submission failed "
                f"for config={self.config_id}: {exc}"
            )

    def stop_live_display(self) -> None:
        dispatcher = self._live_display_dispatcher
        self._live_display_dispatcher = None
        if dispatcher is None:
            return
        stop = getattr(dispatcher, "stop", None)
        if callable(stop):
            stop()

    def capture_recorder_buffer(self, hardware_buffer: np.ndarray, sample_rate: float) -> np.ndarray:
        view = project_selected_channels(
            hardware_buffer,
            self._config.get("selected_channels", []),
        )
        self._buffer = view.buffer
        self._source_channels = view.source_channels
        self._real_to_logical_channel = view.real_to_logical
        self._sample_rate = sample_rate
        return self._buffer

    def _run_judgment(self) -> JudgmentResult | None:
        """Run judgment on the recorded buffer."""
        self._last_ai_review = None
        if self._buffer is None:
            return None

        self._audio_file_path = self._save_wav(self._buffer, self._sample_rate)

        loudness_trace_cache: dict[int, dict] = {}
        result = self._judgment.judge(
            self._config,
            self._buffer,
            self._sample_rate,
            self._mic_cal,
            self._audio_file_path,
            loudness_trace_cache=loudness_trace_cache,
            **self._judgment_channel_kwargs(),
        )
        self._last_loudness_trace_cache = loudness_trace_cache
        result = self._run_ai(result)
        self._last_result = result
        self._persist_audio_result_metadata(result)
        return result

    def _audio_config_metadata(self) -> tuple[str, str | None]:
        config_name = ""
        raw_config_name = self._config.get("config_name")
        if isinstance(raw_config_name, str):
            config_name = raw_config_name.strip()

        if not config_name:
            config_id = self._config.get("config_id")
            if config_id is not None:
                config_name = str(config_id)

        config_snapshot = None
        try:
            config_snapshot = json.dumps(
                self._config,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception as exc:
            self._bus.log.emit(
                f"[TestSession] WARNING: failed to serialize config snapshot "
                f"for config={self.config_id}: {exc}"
            )

        return config_name, config_snapshot

    def _persist_audio_result_metadata(self, result: JudgmentResult) -> None:
        if not self._audio_data_id:
            self._bus.log.emit(
                f"[TestSession] WARNING: audio metadata update skipped: missing audio_data_id "
                f"for config={self.config_id}"
            )
            return

        config_name, config_snapshot = self._audio_config_metadata()
        update_payload = {
            "analyse_config": config_name,
            "config_content": config_snapshot,
        }

        label_value = None
        if result.overall == "合格":
            label_value = "OK"
        elif result.overall == "不合格":
            label_value = "NG"
        else:
            self._bus.log.emit(
                f"[TestSession] WARNING: unexpected judgment overall for config={self.config_id}: "
                f"{result.overall!r}"
            )

        if label_value is not None:
            update_payload["labels"] = label_value
            update_payload["label_source"] = "auto"

        try:
            updated = (
                AudioDataTable
                .update(**update_payload)
                .where(AudioDataTable.audio_data_id == self._audio_data_id)
                .execute()
            )
            if updated != 1:
                self._bus.log.emit(
                    f"[TestSession] WARNING: audio metadata update affected {updated} rows "
                    f"for audio_data_id={self._audio_data_id}"
                )
        except Exception as exc:
            self._bus.log.emit(
                f"[TestSession] WARNING: failed to persist audio result metadata "
                f"for audio_data_id={self._audio_data_id}: {exc}"
            )

    def apply_manual_label(self, label: str) -> ManualLabelUpdateResult:
        normalized = self._normalize_manual_label(label)
        if normalized is None:
            return ManualLabelUpdateResult(False, "无效判定结果")
        if not self._audio_data_id:
            return ManualLabelUpdateResult(False, "当前测试没有已保存的音频记录")

        try:
            updated = (
                AudioDataTable
                .update(labels=normalized, label_source="manual")
                .where(AudioDataTable.audio_data_id == self._audio_data_id)
                .execute()
            )
        except Exception as exc:
            self._bus.log.emit(
                f"[TestSession] WARNING: failed to persist manual judgment "
                f"for audio_data_id={self._audio_data_id}: {exc}"
            )
            return ManualLabelUpdateResult(
                False,
                f"保存人工判定失败：{exc}",
                self._audio_data_id,
            )

        if updated != 1:
            return ManualLabelUpdateResult(False, "当前测试音频记录不存在", self._audio_data_id)
        return ManualLabelUpdateResult(True, "", self._audio_data_id)

    def _run_ai(self, result: JudgmentResult) -> JudgmentResult:
        """Patch AI result into judgment when an enabled binding exists."""
        try:
            binding = self._ai_service.resolve_binding(self.config_id)
        except Exception as exc:
            self._last_ai_review = self._make_ai_review(
                status="binding_error",
                threshold=None,
                score=None,
                channel_index=None,
                error_message=str(exc),
            )
            self._bus.log.emit(
                f"[TestSession] WARNING: failed to resolve AI binding for config={self.config_id}: {exc}"
            )
            return result

        if not binding:
            self._last_ai_review = self._make_ai_review(
                status="not_configured",
                threshold=None,
                score=None,
                channel_index=None,
                error_message=None,
            )
            return result

        if not binding.get("enabled"):
            self._last_ai_review = self._make_ai_review(
                status="disabled",
                threshold=None,
                score=None,
                channel_index=None,
                error_message=None,
            )
            return result

        analysis_start, analysis_end = result.analysis_window
        default_window_offset_ms = int(self._config.get("analysis_window_offset_ms", 0) or 0)
        default_window_duration_ms = int(self._analysis_duration * 1000)
        final_window_offset_ms = int(analysis_start * 1000)
        final_window_duration_ms = max(0, int((analysis_end - analysis_start) * 1000))
        channel_metadata = self._ai_segment_channel_metadata()
        segments = [{
            "segment_id": self._audio_data_id,
            "role": "device_1",
            "audio_path": self._audio_file_path,
            "channels": channel_metadata["channels"],
            "source_channels": channel_metadata["source_channels"],
            "channel_map": channel_metadata["channel_map"],
            "duration_ms": final_window_duration_ms or default_window_duration_ms,
            "sample_rate": int(self._sample_rate),
            "metadata": {},
        }]
        context = {
            "config_id": self.config_id,
            "workorder": self._workorder,
        }
        ai_runs_dir = os.path.abspath(os.path.join(self._audio_dir, "..", "ai_runs"))
        test_run_id = self._get_or_create_test_run_id()
        effective_final_window_duration_ms = final_window_duration_ms or default_window_duration_ms
        window_info = {
            "window_overridden": (
                final_window_offset_ms != default_window_offset_ms
                or effective_final_window_duration_ms != default_window_duration_ms
            ),
            "default_window_offset_ms": default_window_offset_ms,
            "default_window_duration_ms": default_window_duration_ms,
            "final_window_offset_ms": final_window_offset_ms,
            "final_window_duration_ms": effective_final_window_duration_ms,
        }

        try:
            envelope = self._ai_service.run_ai_analysis(
                config_id=self.config_id,
                test_run_id=test_run_id,
                segments=segments,
                context=context,
                ai_runs_dir=ai_runs_dir,
                window_info=window_info,
            )
        except Exception as exc:
            self._last_ai_review = self._make_ai_review(
                status="error",
                threshold=None,
                score=None,
                channel_index=None,
                error_message=str(exc),
            )
            self._bus.log.emit(
                f"[TestSession] WARNING: AI analysis failed for config={self.config_id}: {exc}"
            )
            return result

        if not isinstance(envelope, dict):
            error_message = "AI analysis returned invalid output"
            self._last_ai_review = self._make_ai_review(
                status="invalid_output",
                threshold=None,
                score=None,
                channel_index=None,
                error_message=error_message,
            )
            self._bus.log.emit(
                f"[TestSession] WARNING: AI analysis returned invalid output for config="
                f"{self.config_id}: {envelope!r}"
            )
            return result

        module_status = str(envelope.get("module_status") or "").strip().lower()
        channel_index = self._coerce_ai_channel_index(envelope.get("channel_index"))
        envelope_threshold = self._coerce_ai_score(envelope.get("decision_threshold"))
        if module_status != "success":
            self._last_ai_review = self._make_ai_review(
                status=module_status or "error",
                threshold=envelope_threshold,
                score=self._coerce_ai_score(envelope.get("score")),
                channel_index=channel_index,
                error_message=self._normalize_ai_error(
                    envelope.get("error_summary"),
                    fallback="AI analysis failed",
                ),
            )
            self._bus.log.emit(
                f"[TestSession] WARNING: AI analysis did not succeed for config={self.config_id}: "
                f"module_status={module_status} ai_label={envelope.get('ai_label')} "
                f"error={envelope.get('error_summary')}"
            )
            return result

        effective_threshold = envelope_threshold
        score = self._coerce_ai_score(envelope.get("score"))
        ai_label = envelope.get("ai_label")
        normalized_ai_label = self._normalize_ai_label(ai_label)
        chinese_label_decision = self._ai_pass_from_chinese_label(normalized_ai_label)

        ai_pass = None
        ai_review_status = "success"
        ai_review_error = None

        if score is not None and chinese_label_decision is not None:
            ai_pass = chinese_label_decision
        elif score is not None:
            if effective_threshold is not None:
                ai_pass = (score >= effective_threshold)
            else:
                ai_review_error = "AI decision threshold unavailable"
        elif isinstance(normalized_ai_label, str):
            label = normalized_ai_label.upper()
            if label == "PASS":
                ai_pass = True
                ai_review_status = "missing_score"
                ai_review_error = self._normalize_ai_error(
                    envelope.get("error_summary"),
                    fallback="AI score unavailable",
                )
            elif label == "FAIL":
                ai_pass = False
                ai_review_status = "missing_score"
                ai_review_error = self._normalize_ai_error(
                    envelope.get("error_summary"),
                    fallback="AI score unavailable",
                )
            else:
                ai_review_status = "missing_score"
                ai_review_error = "AI label is not a recognized binary label"
        else:
            ai_review_status = "missing_score"
            ai_review_error = "AI score and usable AI label are unavailable"

        review_threshold = None if chinese_label_decision is not None else effective_threshold
        self._last_ai_review = self._make_ai_review(
            status=ai_review_status,
            threshold=review_threshold,
            score=score,
            channel_index=channel_index,
            error_message=ai_review_error,
            ai_label=normalized_ai_label,
            passed=ai_pass,
        )

        result.ai_pass = ai_pass
        result.ai_score = score
        result.overall = "合格" if result.limit_pass and (ai_pass is True or ai_pass is None) else "不合格"
        return result

    def _save_wav(self, buffer: np.ndarray, sample_rate: float) -> str:
        """Save buffer to WAV file, insert AudioDataTable record, return path."""
        os.makedirs(self._audio_dir, exist_ok=True)
        ts = datetime.now(_TZ8).strftime("%Y%m%d_%H%M%S")
        barcode = self._workorder.get("sn", "") or uuid.uuid4().hex[:8]
        filename = f"{self.config_id}_{ts}_{barcode}.wav"
        filepath = os.path.abspath(os.path.join(self._audio_dir, filename))
        data = buffer.astype(np.float32)
        wavfile.write(filepath, int(sample_rate), data)

        audio_id = uuid.uuid4().hex
        config_name, config_snapshot = self._audio_config_metadata()
        AudioDataTable.create(
            audio_data_id=audio_id,
            file_path=filepath,
            product_model=self._workorder.get("model", ""),
            sample_rate=int(sample_rate),
            record_date=datetime.now(_TZ8).isoformat(),
            labels=None,
            barcode=self._workorder.get("sn", ""),
            analyse_config=config_name,
            config_content=config_snapshot,
        )
        self._audio_data_id = audio_id
        return filepath

    def rejudge(self, start_sec: float, end_sec: float) -> JudgmentResult | None:
        """Re-run judgment on a user-selected window."""
        if self._buffer is None or self._audio_file_path is None:
            return None
        self._last_ai_review = None
        loudness_trace_cache: dict[int, dict] = {}
        result = self._judgment.rejudge(
            self._config,
            self._buffer,
            self._sample_rate,
            self._mic_cal,
            self._audio_file_path,
            start_sec,
            end_sec,
            loudness_trace_cache=loudness_trace_cache,
            **self._judgment_channel_kwargs(),
        )
        self._last_loudness_trace_cache = loudness_trace_cache
        result = self._run_ai(result)
        self._last_result = result
        return result

    def _judgment_channel_kwargs(self) -> dict[str, dict[int, int]]:
        if self._real_to_logical_channel:
            return {"channel_column_map": dict(self._real_to_logical_channel)}
        return {}

    def _ai_segment_channel_metadata(self) -> dict[str, list[Any]]:
        if self._source_channels:
            source_channels = list(self._source_channels)
            logical_channels = [
                int(self._real_to_logical_channel.get(source, fallback))
                for fallback, source in enumerate(source_channels)
            ]
        elif self._real_to_logical_channel:
            pairs = sorted(
                self._real_to_logical_channel.items(),
                key=lambda item: item[1],
            )
            source_channels = [int(source) for source, _ in pairs]
            logical_channels = [int(logical) for _, logical in pairs]
        else:
            source_channels, logical_channels = self._fallback_ai_segment_channels()

        if len(source_channels) != len(logical_channels):
            source_channels = source_channels[:len(logical_channels)]
        if len(source_channels) < len(logical_channels):
            source_channels = list(logical_channels)

        return {
            "channels": logical_channels,
            "source_channels": source_channels,
            "channel_map": [
                {"logical_channel": logical, "source_channel": source}
                for logical, source in zip(logical_channels, source_channels)
            ],
        }

    def _fallback_ai_segment_channels(self) -> tuple[list[int], list[int]]:
        selected_channels = self._selected_channels_for_ai_metadata()
        buffer_channel_count = self._buffer_channel_count()

        if buffer_channel_count <= 0:
            logical_channels = list(range(len(selected_channels)))
        elif selected_channels and all(
            channel < buffer_channel_count for channel in selected_channels
        ):
            logical_channels = list(selected_channels)
        else:
            logical_channel_count = buffer_channel_count
            if selected_channels:
                logical_channel_count = min(buffer_channel_count, len(selected_channels))
            logical_channels = list(range(logical_channel_count))

        if selected_channels:
            source_channels = selected_channels[:len(logical_channels)]
        else:
            source_channels = list(logical_channels)
        return source_channels, logical_channels

    def _selected_channels_for_ai_metadata(self) -> list[int]:
        selected_channels: list[int] = []
        for channel in self._config.get("selected_channels", []) or ():
            if isinstance(channel, (bool, np.bool_)):
                continue
            if isinstance(channel, (int, np.integer)) and int(channel) >= 0:
                selected_channels.append(int(channel))
        return selected_channels

    def _buffer_channel_count(self) -> int:
        if self._buffer is None:
            return 0
        if self._buffer.ndim == 1:
            return 1
        if self._buffer.ndim == 2:
            return int(self._buffer.shape[1])
        return 0

    def _get_or_create_test_run_id(self) -> str:
        """Return the stable AI test_run_id for this session."""
        if self._test_run_id is None:
            timestamp_ms = int(datetime.now(_TZ8).timestamp() * 1000)
            self._test_run_id = f"run_{self.config_id}_{timestamp_ms}"
        return self._test_run_id

    def _runtime_frame_run_id(self) -> str:
        workorder = self._workorder
        if isinstance(workorder, dict):
            factory_run_id = workorder.get("factory_run_id")
            if isinstance(factory_run_id, str) and factory_run_id:
                return factory_run_id
        return self._get_or_create_test_run_id()

    def _get_sensitivity(self, channel_index: int) -> float:
        """Get mic sensitivity (Pa/V) for a channel from mic_cal."""
        key = f"In{channel_index + 1}"
        entry = self._mic_cal.get(key, {})
        raw = entry.get("deviation_value", "0")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0

    @property
    def last_result(self) -> JudgmentResult | None:
        return self._last_result

    @property
    def last_ai_review(self) -> dict[str, Any] | None:
        return self._last_ai_review

    @property
    def buffer(self) -> np.ndarray | None:
        return self._buffer

    @staticmethod
    def _coerce_ai_score(value) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if math.isfinite(value):
                return float(value)
            return None
        return None

    @staticmethod
    def _normalize_manual_label(label: str) -> str | None:
        text = str(label or "").strip().upper()
        if text in {"OK", "NG"}:
            return text
        return None

    def _default_live_loudness_channel(self, selected_channels) -> int | None:
        return resolve_layout_channel(
            CHANNEL_PRIMARY_ACOUSTIC,
            selected_channels=self._normalized_channel_indexes(selected_channels),
            vibration_view_channel_index=self._as_non_bool_int(
                self._config.get("vibration_view_channel_index")
            ),
            panel_family=PANEL_FAMILY_ACOUSTIC,
        )

    @classmethod
    def _normalized_channel_indexes(cls, channels) -> list[int]:
        normalized_channels = []
        for channel in channels or ():
            normalized = cls._as_non_bool_int(channel)
            if normalized is not None:
                normalized_channels.append(normalized)
        return normalized_channels

    @staticmethod
    def _as_non_bool_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _live_display_submit_accepts_sample_span(submit) -> bool:
        try:
            signature = inspect.signature(submit)
        except (TypeError, ValueError):
            return True
        parameters = signature.parameters
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return True
        return "sample_start" in parameters and "sample_end" in parameters

    @staticmethod
    def _coerce_ai_channel_index(value) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    @staticmethod
    def _normalize_ai_error(message, *, fallback: str | None = None) -> str | None:
        if isinstance(message, str):
            normalized = message.strip()
            if normalized:
                return normalized
        return fallback

    @staticmethod
    def _normalize_ai_label(label) -> str | None:
        if not isinstance(label, str):
            return None
        normalized = label.strip()
        return normalized or None

    @classmethod
    def _ai_pass_from_chinese_label(cls, label: str | None) -> bool | None:
        if label is None or not cls._contains_cjk(label):
            return None
        normalized = label.strip()
        if normalized in {"通过", "合格", "正常"}:
            return True
        return False

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    @staticmethod
    def _make_ai_review(
        *,
        status: str,
        threshold: float | None,
        score: float | None,
        channel_index: int | None,
        error_message: str | None,
        ai_label: str | None = None,
        passed: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "threshold": threshold,
            "score": score,
            "channel_index": channel_index,
            "error_message": error_message,
            "ai_label": ai_label,
            "passed": passed,
        }
