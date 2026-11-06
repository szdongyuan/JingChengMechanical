"""Best-effort live display analysis outside the recorder critical path."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.service.analysis.stft_preview import StftPreviewReducer
from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_BELOW_NORMAL,
    set_current_thread_priority,
)


_SPL_EMIT_THROTTLE_SEC = 0.25
_SPL_EMIT_EPSILON_SEC = 1e-9


@dataclass(frozen=True)
class _DisplayWork:
    chunk: np.ndarray
    elapsed_time: float
    frame_run_id: str
    live_loudness_channel_index: int | None
    sample_start: int | None
    sample_end: int | None
    generation: int
    source_continuity: str | None = None


@dataclass(frozen=True)
class _LoudnessWork:
    pipeline: Any
    loudness_input: np.ndarray
    elapsed_time: float
    frame_run_id: str
    channel_index: int
    continuity_generation: int
    generation: int


@dataclass(frozen=True)
class _PreviewIdentity:
    """Delivery identity without retaining the source work's PCM chunk."""

    frame_run_id: str
    generation: int


@dataclass
class _PendingPreview:
    reducer: StftPreviewReducer
    identity: _PreviewIdentity
    deadline: float


class LiveDisplayDispatcher:
    """Run live display pipelines on a bounded FIFO background worker."""

    def __init__(
        self,
        *,
        pipelines,
        bus,
        config_id: int,
        max_queue_size: int = 8,
        coalesce_pending: bool = False,
        stft_emit_interval_sec: float = 0.0,
        coarse_stft: bool = False,
    ):
        self._pipelines = list(pipelines)
        self._bus = bus
        self._config_id = config_id
        self._max_queue_size = max(1, int(max_queue_size))
        self._coalesce_pending = bool(coalesce_pending)
        self._stft_emit_interval_sec = max(0.0, float(stft_emit_interval_sec))
        self._coarse_stft = bool(coarse_stft)
        self._coarse_interval_sec = self._stft_emit_interval_sec or 0.1
        if self._coarse_stft and not np.isfinite(self._coarse_interval_sec):
            raise ValueError("coarse STFT interval must be finite")
        self._coarse_pending: dict[int, _PendingPreview] = {}
        self._coarse_drain_requested = False
        self._last_preview_identity: tuple[str, str | None] | None = None
        self._condition = threading.Condition()
        self._queue: deque[_DisplayWork] = deque()
        self._pending_loudness: _LoudnessWork | None = None
        self._stopping = False
        self._closed = False
        self._generation = 0
        self._continuity_generation = 0
        self._last_processed_sample_end: int | None = None
        self._last_source_continuity: str | None = None
        self._last_spl_emit_time_by_key: dict[tuple[int, str, int, int], float] = {}
        self._last_stft_emit_time_by_key: dict[tuple[int, str, int, int], float] = {}
        self._overflow_log_active = False
        self._active_work = False
        self._thread: threading.Thread | None = None
        self._loudness_thread: threading.Thread | None = None
        self._emit_stop_lock = threading.RLock()

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._queue)

    def start(self) -> None:
        self._ensure_started()

    def _ensure_started(self) -> bool:
        with self._condition:
            if self._closed:
                return False
            if self._thread is not None and self._thread.is_alive():
                display_running = True
            else:
                display_running = False
                self._stopping = False
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"LiveDisplayDispatcher-{self._config_id}",
                    daemon=True,
                )
                self._thread.start()
            if self._loudness_thread is None or not self._loudness_thread.is_alive():
                self._stopping = False
                self._loudness_thread = threading.Thread(
                    target=self._run_loudness,
                    name=f"LiveDisplayLoudness-{self._config_id}",
                    daemon=True,
                )
                self._loudness_thread.start()
            return display_running or self._thread is not None

    def submit(
        self,
        chunk: np.ndarray,
        *,
        elapsed_time: float,
        frame_run_id: str,
        live_loudness_channel_index: int | None,
        sample_start: int | None = None,
        sample_end: int | None = None,
        source_continuity: str | None = None,
    ) -> bool:
        if not self._ensure_started():
            return False
        chunk_copy = np.asarray(chunk).copy()
        should_log_overflow = False
        with self._condition:
            if self._closed or self._stopping:
                return False
            work = _DisplayWork(
                chunk=chunk_copy,
                elapsed_time=float(elapsed_time),
                frame_run_id=str(frame_run_id),
                live_loudness_channel_index=live_loudness_channel_index,
                sample_start=self._normalize_sample_boundary(sample_start),
                sample_end=self._normalize_sample_boundary(sample_end),
                generation=self._generation,
                source_continuity=source_continuity,
            )
            dropped_for_overflow = False
            if not self._coalesce_pending:
                while len(self._queue) >= self._max_queue_size:
                    self._queue.popleft()
                    dropped_for_overflow = True
            if dropped_for_overflow and not self._overflow_log_active:
                self._overflow_log_active = True
                should_log_overflow = True
            self._queue.append(work)
            self._condition.notify_all()
        if should_log_overflow:
            self._log_queue_overflow()
        return True

    def stop(self, timeout: float = 1.0) -> bool:
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        with self._emit_stop_lock:
            with self._condition:
                self._closed = True
                self._stopping = True
                self._generation += 1
                self._continuity_generation += 1
                self._queue.clear()
                self._pending_loudness = None
                self._coarse_pending.clear()
                self._coarse_drain_requested = False
                self._last_preview_identity = None
                self._last_processed_sample_end = None
                self._last_spl_emit_time_by_key.clear()
                self._last_stft_emit_time_by_key.clear()
                self._overflow_log_active = False
                self._condition.notify_all()
                thread = self._thread
                loudness_thread = self._loudness_thread

        timed_out = False
        current_thread = threading.current_thread()
        for worker in (thread, loudness_thread):
            if worker is None:
                continue
            if worker is not current_thread:
                worker.join(max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                timed_out = True
        if timed_out:
            self._log_stop_timeout(timeout)
        return not timed_out

    def _run(self) -> None:
        set_current_thread_priority(THREAD_PRIORITY_BELOW_NORMAL)
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    if self._coarse_pending:
                        remaining = min(p.deadline for p in self._coarse_pending.values()) - time.monotonic()
                        if self._coarse_drain_requested or remaining <= 0:
                            break
                        self._condition.wait(remaining)
                    else:
                        self._condition.wait()
                if not self._queue and self._stopping:
                    return
                work = self._queue.popleft() if self._queue else None
                if work is not None and self._coalesce_pending:
                    work = self._coalesce_contiguous_pending_locked(work)
                self._active_work = True
                if not self._queue:
                    self._overflow_log_active = False

            try:
                if work is None:
                    self._flush_coarse_pending(due_only=not self._coarse_drain_requested)
                    continue
                if not self._work_is_current(work):
                    continue
                if self._work_has_sample_gap(work):
                    self._flush_coarse_pending()
                    self._reset_pipelines_for_gap()
                self._process_work(work)
                self._record_processed_sample_end(work)
                self._flush_coarse_pending(due_only=True)
            finally:
                with self._condition:
                    self._active_work = False
                    self._condition.notify_all()

    def _coalesce_contiguous_pending_locked(
        self,
        work: _DisplayWork,
    ) -> _DisplayWork:
        """Merge queued chunks from the same run before doing display DSP.

        This runs only on the display worker.  It preserves every captured
        sample while avoiding repeated FFT/STFT setup when rendering falls
        behind capture.
        """
        items = [work]
        last = work
        while self._queue:
            candidate = self._queue[0]
            if not self._work_is_contiguous(last, candidate):
                break
            self._queue.popleft()
            items.append(candidate)
            last = candidate
        if len(items) == 1:
            return work
        return self._merge_work_items(items)

    @staticmethod
    def _merge_work_items(items) -> _DisplayWork:
        items = tuple(items)
        first = items[0]
        last = items[-1]
        return _DisplayWork(
            chunk=np.concatenate([item.chunk for item in items], axis=0),
            elapsed_time=last.elapsed_time,
            frame_run_id=first.frame_run_id,
            live_loudness_channel_index=first.live_loudness_channel_index,
            sample_start=first.sample_start,
            sample_end=last.sample_end,
            generation=first.generation,
            source_continuity=first.source_continuity,
        )

    def wait_until_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            if self._coarse_stft:
                self._coarse_drain_requested = True
                self._condition.notify_all()
            while self._queue or self._active_work or self._coarse_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            self._coarse_drain_requested = False
            return True

    @staticmethod
    def _work_is_contiguous(left: _DisplayWork, right: _DisplayWork) -> bool:
        return bool(
            left.generation == right.generation
            and left.source_continuity == right.source_continuity
            and left.frame_run_id == right.frame_run_id
            and left.live_loudness_channel_index
            == right.live_loudness_channel_index
            and left.sample_end is not None
            and right.sample_start == left.sample_end
            and left.chunk.ndim == right.chunk.ndim
            and (
                left.chunk.ndim != 2
                or left.chunk.shape[1] == right.chunk.shape[1]
            )
        )

    def _run_loudness(self) -> None:
        set_current_thread_priority(THREAD_PRIORITY_BELOW_NORMAL)
        while True:
            with self._condition:
                while self._pending_loudness is None and not self._stopping:
                    self._condition.wait()
                if self._pending_loudness is None and self._stopping:
                    return
                work = self._pending_loudness
                self._pending_loudness = None

            if work is None:
                continue
            if not self._loudness_work_is_current(work):
                continue
            self._process_loudness_work(work)

    def _process_work(self, work: _DisplayWork) -> None:
        for pipeline in list(self._pipelines):
            channel_index = getattr(pipeline, "channel_index", None)
            if not isinstance(channel_index, int):
                continue
            mono = self._extract_channel(work.chunk, channel_index)
            if mono is None:
                continue
            try:
                result = pipeline.process_chunk(
                    mono,
                    work.elapsed_time,
                    compute_loudness=False,
                )
            except Exception as exc:
                self._log_display_error(channel_index, exc)
                continue
            self._emit_result(channel_index, work, result, pipeline=pipeline)
            if channel_index == work.live_loudness_channel_index:
                self._submit_loudness_input(pipeline, channel_index, work)

    def _submit_loudness_input(
        self,
        pipeline: Any,
        channel_index: int,
        work: _DisplayWork,
    ) -> None:
        prepare = getattr(pipeline, "prepare_live_loudness_input", None)
        if not callable(prepare):
            return
        try:
            loudness_input = prepare(work.elapsed_time)
        except Exception as exc:
            self._log_display_error(channel_index, exc)
            return
        if loudness_input is None:
            return

        loudness_copy = np.asarray(loudness_input, dtype=np.float64).copy()
        with self._condition:
            if (
                self._closed
                or self._stopping
                or work.generation != self._generation
            ):
                return
            self._pending_loudness = _LoudnessWork(
                pipeline=pipeline,
                loudness_input=loudness_copy,
                elapsed_time=work.elapsed_time,
                frame_run_id=work.frame_run_id,
                channel_index=channel_index,
                continuity_generation=self._continuity_generation,
                generation=work.generation,
            )
            self._condition.notify_all()

    def _process_loudness_work(self, work: _LoudnessWork) -> None:
        compute = getattr(work.pipeline, "compute_live_loudness_frame", None)
        if not callable(compute):
            return
        try:
            frame = compute(work.loudness_input, work.elapsed_time)
        except Exception:
            return
        if frame is None:
            return
        self._emit_loudness_for_work(work, frame)

    @staticmethod
    def _extract_channel(chunk: np.ndarray, channel_index: int) -> np.ndarray | None:
        if chunk.ndim == 1:
            return chunk
        if chunk.ndim == 2 and 0 <= channel_index < chunk.shape[1]:
            return chunk[:, channel_index]
        return None

    def _emit_result(self, channel_index: int, work: _DisplayWork, result: Any, *, pipeline=None) -> None:
        if not isinstance(result, dict):
            return
        if not self._work_is_current(work):
            return

        for stft_frame in result.get("stft_frames") or []:
            if self._coarse_stft:
                self._reduce_stft_for_work(pipeline, channel_index, work, stft_frame)
                continue
            if not self._should_emit_stft_for_work(
                work,
                channel_index,
                stft_frame,
            ):
                continue
            self._emit_for_work(
                work,
                "stft_updated",
                self._config_id,
                work.frame_run_id,
                channel_index,
                stft_frame,
            )
        if result.get("spl") is not None:
            self._emit_spl_for_work(
                work,
                channel_index,
                result["spl"],
            )
        if result.get("loudness") is not None:
            self._emit_for_work(
                work,
                "loudness_updated",
                self._config_id,
                work.frame_run_id,
                channel_index,
                result["loudness"],
            )
        if result.get("fft") is not None:
            self._emit_for_work(
                work,
                "fft_updated",
                self._config_id,
                work.frame_run_id,
                channel_index,
                result["fft"],
            )

    def _reduce_stft_for_work(self, pipeline, channel_index, work, frame) -> None:
        try:
            # Match AnalysisPipeline's integer overlap rounding exactly.
            hop_sec = (pipeline._nfft - int(pipeline._nfft * pipeline._overlap)) / pipeline.sample_rate
            with self._condition:
                if not self._work_is_current(work):
                    return
                pending = self._coarse_pending.get(channel_index)
                if pending is None:
                    pending = _PendingPreview(
                        StftPreviewReducer(interval_sec=self._coarse_interval_sec),
                        _PreviewIdentity(work.frame_run_id, work.generation),
                        time.monotonic() + self._coarse_interval_sec,
                    )
                frames = pending.reducer.push(frame, hop_sec=hop_sec)
                if pending.reducer.pending:
                    if frames:
                        pending.deadline = time.monotonic() + self._coarse_interval_sec
                    self._coarse_pending[channel_index] = pending
                else:
                    self._coarse_pending.pop(channel_index, None)
                self._condition.notify_all()
            for reduced in frames:
                self._emit_for_work(pending.identity, "stft_updated", self._config_id,
                                    pending.identity.frame_run_id, channel_index, reduced)
        except (AttributeError, TypeError, ValueError, ZeroDivisionError) as exc:
            self._log_display_error(channel_index, exc)

    def _flush_coarse_pending(self, *, due_only=False) -> None:
        with self._condition:
            ready = [(channel, pending) for channel, pending in self._coarse_pending.items()
                     if not due_only or pending.deadline <= time.monotonic()]
            for channel, _ in ready:
                del self._coarse_pending[channel]
        for channel, pending in ready:
            for frame in pending.reducer.flush():
                self._emit_for_work(pending.identity, "stft_updated", self._config_id,
                                    pending.identity.frame_run_id, channel, frame)

    def _emit_for_work(self, work: _DisplayWork | _PreviewIdentity, signal_name: str, *args) -> None:
        with self._emit_stop_lock:
            if not self._work_is_current(work):
                return
            signal = getattr(self._bus, signal_name, None)
            emit = getattr(signal, "emit", None)
            if not callable(emit):
                return
            if not self._work_is_current(work):
                return
            emit(*args)

    def _emit_spl_for_work(
        self,
        work: _DisplayWork,
        channel_index: int,
        frame: Any,
    ) -> None:
        if not self._should_emit_spl_for_work(work, channel_index, frame):
            return
        self._emit_for_work(
            work,
            "spl_updated",
            self._config_id,
            work.frame_run_id,
            channel_index,
            frame,
        )

    def _should_emit_spl_for_work(
        self,
        work: _DisplayWork,
        channel_index: int,
        frame: Any,
    ) -> bool:
        frame_time = self._spl_frame_time(frame, work.elapsed_time)
        with self._condition:
            if (
                self._closed
                or self._stopping
                or work.generation != self._generation
            ):
                return False
            key = (
                self._config_id,
                work.frame_run_id,
                channel_index,
                self._continuity_generation,
            )
            last_emit_time = self._last_spl_emit_time_by_key.get(key)
            if (
                last_emit_time is not None
                and frame_time - last_emit_time < _SPL_EMIT_THROTTLE_SEC - _SPL_EMIT_EPSILON_SEC
            ):
                return False
            self._last_spl_emit_time_by_key[key] = frame_time
            return True

    def _should_emit_stft_for_work(
        self,
        work: _DisplayWork,
        channel_index: int,
        frame: Any,
    ) -> bool:
        if self._stft_emit_interval_sec <= 0.0:
            return True
        frame_time = self._spl_frame_time(frame, work.elapsed_time)
        with self._condition:
            if (
                self._closed
                or self._stopping
                or work.generation != self._generation
            ):
                return False
            key = (
                self._config_id,
                work.frame_run_id,
                channel_index,
                self._continuity_generation,
            )
            last_emit_time = self._last_stft_emit_time_by_key.get(key)
            if (
                last_emit_time is not None
                and frame_time - last_emit_time
                < self._stft_emit_interval_sec - _SPL_EMIT_EPSILON_SEC
            ):
                return False
            self._last_stft_emit_time_by_key[key] = frame_time
            return True

    @staticmethod
    def _spl_frame_time(frame: Any, fallback: float) -> float:
        raw_time = getattr(frame, "time", fallback)
        try:
            frame_time = float(raw_time)
        except (TypeError, ValueError):
            frame_time = float(fallback)
        if not np.isfinite(frame_time):
            return float(fallback)
        return frame_time

    def _emit_loudness_for_work(self, work: _LoudnessWork, frame: Any) -> None:
        with self._emit_stop_lock:
            if not self._loudness_work_is_current(work):
                return
            signal = getattr(self._bus, "loudness_updated", None)
            emit = getattr(signal, "emit", None)
            if not callable(emit):
                return
            if not self._loudness_work_is_current(work):
                return
            emit(
                self._config_id,
                work.frame_run_id,
                work.channel_index,
                frame,
            )

    def _work_is_current(self, work: _DisplayWork | _PreviewIdentity) -> bool:
        with self._condition:
            return (
                (not self._closed)
                and (not self._stopping)
                and work.generation == self._generation
            )

    def _loudness_work_is_current(self, work: _LoudnessWork) -> bool:
        with self._condition:
            return (
                (not self._closed)
                and (not self._stopping)
                and work.generation == self._generation
                and work.continuity_generation == self._continuity_generation
            )

    def _work_has_sample_gap(self, work: _DisplayWork) -> bool:
        with self._condition:
            gap_detected = (
                self._last_processed_sample_end is not None
                and work.sample_start is not None
                and work.sample_start != self._last_processed_sample_end
            ) or (
                self._last_source_continuity is not None
                and work.source_continuity != self._last_source_continuity
            ) or (
                self._coarse_stft and self._last_preview_identity is not None
                and (work.frame_run_id, work.source_continuity) != self._last_preview_identity
            )
            if gap_detected:
                self._continuity_generation += 1
                self._pending_loudness = None
                self._last_spl_emit_time_by_key.clear()
                self._last_stft_emit_time_by_key.clear()
                self._condition.notify_all()
            return gap_detected

    def _record_processed_sample_end(self, work: _DisplayWork) -> None:
        with self._condition:
            if (
                (not self._closed)
                and (not self._stopping)
                and work.generation == self._generation
            ):
                if self._coarse_stft:
                    self._last_preview_identity = (work.frame_run_id, work.source_continuity)
                if work.sample_end is not None:
                    self._last_processed_sample_end = work.sample_end
                    self._last_source_continuity = work.source_continuity

    def _reset_pipelines_for_gap(self) -> None:
        for pipeline in list(self._pipelines):
            reset = getattr(pipeline, "reset", None)
            if not callable(reset):
                continue
            try:
                reset()
            except Exception as exc:
                channel_index = getattr(pipeline, "channel_index", -1)
                if isinstance(channel_index, int):
                    self._log_display_error(channel_index, exc)

    @staticmethod
    def _normalize_sample_boundary(value: int | None) -> int | None:
        if value is None:
            return None
        return int(value)

    def _log_display_error(self, channel_index: int, exc: Exception) -> None:
        log = getattr(getattr(self._bus, "log", None), "emit", None)
        if callable(log):
            log(
                f"[LiveDisplayDispatcher] WARNING: display analysis failed "
                f"for config={self._config_id} channel=CH{channel_index + 1}: {exc}"
            )

    def _log_queue_overflow(self) -> None:
        log = getattr(getattr(self._bus, "log", None), "emit", None)
        if callable(log):
            log(
                f"[LiveDisplayDispatcher] WARNING: display queue overflow "
                f"for config={self._config_id}; dropped older pending display chunks"
            )

    def _log_stop_timeout(self, timeout: float) -> None:
        log = getattr(getattr(self._bus, "log", None), "emit", None)
        if callable(log):
            log(
                f"[LiveDisplayDispatcher] WARNING: display worker did not stop "
                f"within {timeout:.3g}s for config={self._config_id}"
            )
