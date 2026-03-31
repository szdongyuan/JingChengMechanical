"""Test session — one test run lifecycle for a single config.

Orchestrates: recorder -> pipelines -> judgment -> result.
"""
from __future__ import annotations

import math
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from scipy.io import wavfile

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.judgment import JudgmentEngine, _runtime_channel_defaults
from app.service.analysis.pipeline import AnalysisPipeline
from model.audio_data_table import AudioDataTable

_TZ8 = timezone(timedelta(hours=8))


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
        self._buffer: np.ndarray | None = None
        self._judgment: JudgmentEngine = JudgmentEngine(ai_service)
        self._last_result: JudgmentResult | None = None
        self._last_ai_review: dict[str, Any] | None = None
        self._audio_file_path: str | None = None
        self._audio_data_id: str | None = None
        self._test_run_id: str | None = None
        self._finished: bool = False
        self._attach_chunk_index: int = 0
        self._attach_snapshot: Any | None = None
        self._processed_samples_since_attach: int = 0
        self._attach_pending: bool = False
        self._pending_attach_chunks: list[tuple[np.ndarray, int]] = []
        self._on_session_done_cb: Callable[[int], Any] | None = None

        # Connection refs set by TestManager for signal disconnection
        self._chunk_conn: Callable | None = None
        self._done_lambda: Callable | None = None
        self._error_lambda: Callable | None = None
        self._monitor_lambda: Callable | None = None
        self._monitor_error_lambda: Callable | None = None

    def _setup_pipelines(self):
        """Create one AnalysisPipeline per selected channel."""
        selected = self._config.get("selected_channels", [])
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

    def prepare_for_attach(self):
        """Buffer chunks until the recorder-owned attach boundary is known."""
        self._attach_pending = True
        self._pending_attach_chunks = []

    def attach_to_recorder(self, *, chunk_index: int, snapshot: Any | None = None):
        """Record the chunk boundary where this session becomes live."""
        self._attach_chunk_index = max(0, int(chunk_index))
        self._attach_snapshot = snapshot
        self._processed_samples_since_attach = 0
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

    def _process_chunk(self, chunk: np.ndarray, chunk_index: int):
        if chunk_index < self._attach_chunk_index:
            return

        chunk_samples = int(chunk.shape[0]) if chunk.ndim > 0 else 0
        self._processed_samples_since_attach += max(0, chunk_samples)
        elapsed = (
            self._processed_samples_since_attach / self._sample_rate
            if self._sample_rate > 0
            else 0.0
        )
        frame_run_id = self._runtime_frame_run_id()

        for pipeline in self._pipelines:
            ch_idx = pipeline.channel_index
            if chunk.ndim == 2 and ch_idx < chunk.shape[1]:
                mono = chunk[:, ch_idx]
            elif chunk.ndim == 1:
                mono = chunk
            else:
                continue

            result = pipeline.process_chunk(mono, elapsed)

            for stft_frame in result["stft_frames"]:
                self._bus.stft_updated.emit(self.config_id, frame_run_id, ch_idx, stft_frame)
            if result["spl"] is not None:
                self._bus.spl_updated.emit(self.config_id, frame_run_id, ch_idx, result["spl"])
            if result["octave"] is not None:
                self._bus.octave_updated.emit(self.config_id, frame_run_id, ch_idx, result["octave"])
            if result["fft"] is not None:
                self._bus.fft_updated.emit(self.config_id, frame_run_id, ch_idx, result["fft"])

        self._bus.recording_progress.emit(self.config_id, elapsed)

        if not self._finished and elapsed >= self._record_duration:
            self._finished = True
            if self._on_session_done_cb is not None:
                self._on_session_done_cb(self.config_id)

    def _run_judgment(self) -> JudgmentResult | None:
        """Run judgment on the recorded buffer."""
        self._last_ai_review = None
        if self._buffer is None:
            return None

        self._audio_file_path = self._save_wav(self._buffer, self._sample_rate)

        result = self._judgment.judge(
            self._config,
            self._buffer,
            self._sample_rate,
            self._mic_cal,
            self._audio_file_path,
        )
        result = self._run_ai(result)
        self._last_result = result
        return result

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
                threshold=self._coerce_ai_score(binding.get("score_threshold")),
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
        segments = [{
            "segment_id": self._audio_data_id,
            "role": "device_1",
            "audio_path": self._audio_file_path,
            "channels": self._config.get("selected_channels", []),
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
        score_threshold = self._coerce_ai_score(binding.get("score_threshold"))
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
                threshold=score_threshold,
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
                threshold=score_threshold,
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
        if module_status != "success":
            self._last_ai_review = self._make_ai_review(
                status=module_status or "error",
                threshold=score_threshold,
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

        score = self._coerce_ai_score(envelope.get("score"))
        ai_label = envelope.get("ai_label")

        # Primary: score + threshold
        ai_pass = None
        if score is not None and score_threshold is not None:
            ai_pass = (score >= score_threshold)
        # Fallback: ai_label (legacy toolchains without numeric score)
        elif isinstance(ai_label, str):
            label = ai_label.strip().upper()
            if label == "PASS":
                ai_pass = True
            elif label == "FAIL":
                ai_pass = False

        ai_review_status = "success"
        ai_review_error = None
        if score is None and (score_threshold is not None or ai_pass is None):
            ai_review_status = "missing_score"
            ai_review_error = self._normalize_ai_error(
                envelope.get("error_summary"),
                fallback="AI score unavailable",
            )

        self._last_ai_review = self._make_ai_review(
            status=ai_review_status,
            threshold=score_threshold,
            score=score,
            channel_index=channel_index,
            error_message=ai_review_error,
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
        AudioDataTable.create(
            audio_data_id=audio_id,
            file_path=filepath,
            product_model=self._workorder.get("model", ""),
            sample_rate=int(sample_rate),
            record_date=datetime.now(_TZ8).isoformat(),
            labels=None,
            barcode=self._workorder.get("sn", ""),
        )
        self._audio_data_id = audio_id
        return filepath

    def rejudge(self, start_sec: float, end_sec: float) -> JudgmentResult | None:
        """Re-run judgment on a user-selected window."""
        if self._buffer is None or self._audio_file_path is None:
            return None
        self._last_ai_review = None
        result = self._judgment.rejudge(
            self._config,
            self._buffer,
            self._sample_rate,
            self._mic_cal,
            self._audio_file_path,
            start_sec,
            end_sec,
        )
        result = self._run_ai(result)
        self._last_result = result
        return result

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
    def _make_ai_review(
        *,
        status: str,
        threshold: float | None,
        score: float | None,
        channel_index: int | None,
        error_message: str | None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "threshold": threshold,
            "score": score,
            "channel_index": channel_index,
            "error_message": error_message,
        }
