"""Audio recording via sounddevice with ring buffer.

Runs a sounddevice InputStream in callback mode. A QThread drains chunks
from a queue and emits them as Qt signals for real-time analysis.
"""
from __future__ import annotations

import logging
import math
import operator
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread, pyqtSignal

from app.service.analysis.session_capture import (
    SESSION_WARMUP_SECONDS,
    CaptureNotAcceptedError,
    CapturedAudioBlock,
    RecorderAttachment,
    ResolvedCaptureBounds,
    frames_for_duration,
)

os.environ.setdefault("SD_ENABLE_ASIO", "1")

logger = logging.getLogger(__name__)


def _resolve_device_index(device_name: str, hostapi: str) -> int:
    hostapis = sd.query_hostapis()
    hostapi_idx = None
    for i, api in enumerate(hostapis):
        if api["name"] == hostapi:
            hostapi_idx = i
            break
    if hostapi_idx is None:
        raise ValueError(f"Host API not found: {hostapi}")
    for i, dev in enumerate(sd.query_devices()):
        if dev["name"] == device_name and dev["hostapi"] == hostapi_idx:
            return i
    raise ValueError(f"Device not found: {device_name} ({hostapi})")


@dataclass(frozen=True)
class RecorderAttachSnapshot:
    chunk_index: int
    sample_index: int


class AudioRecorder(QThread):
    """Record multi-channel audio into a pre-allocated ring buffer.

    Signals
    -------
    captured_block_ready(CapturedAudioBlock)
        Emitted with the exact callback-owned sample and monotonic span.
    chunk_ready(ndarray, int)
        Temporary legacy emission of (chunk_data, chunk_index).
    recording_done()
        Emitted once the buffer is full.
    error_occurred(str)
        Emitted on device errors.
    """

    chunk_ready = pyqtSignal(object, int)
    captured_block_ready = pyqtSignal(object)
    recording_done = pyqtSignal()
    error_occurred = pyqtSignal(str)

    CHUNK_DURATION = 0.05

    def __init__(
        self,
        device_name: str,
        hostapi: str,
        sample_rate: float,
        n_channels: int,
        record_duration: float,
        monotonic_func: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        self._device_name = device_name
        self._hostapi = hostapi
        self._sample_rate = sample_rate
        self._n_channels = n_channels
        self._record_duration = record_duration
        self._monotonic_func = monotonic_func

        total_samples = int(sample_rate * record_duration)
        self._buffer = np.zeros((total_samples, n_channels), dtype=np.float32)
        self._legacy_target_samples = total_samples
        self._write_pos = 0
        self._capture_sample_cursor = 0
        self._chunk_samples = int(sample_rate * self.CHUNK_DURATION)
        self._chunk_index = 0
        self._emitted_chunk_index = 0
        self._emitted_sample_index = 0
        self._state_lock = threading.Lock()

        self._queue: queue.Queue[CapturedAudioBlock] = queue.Queue()
        self._stop_flag = False
        self._accepting_captures = True
        self._pending_capture_owners: dict[
            RecorderAttachment, RecorderAttachment
        ] = {}
        self._active_capture_owners: dict[
            RecorderAttachment, RecorderAttachment
        ] = {}
        self._timing_degradation_logged = False

    def extend_duration(self, record_duration: float):
        """Grow the backing buffer to a longer duration if needed."""
        with self._state_lock:
            self._ensure_duration_locked(record_duration)

    def attach_session(self, *, record_duration: float) -> RecorderAttachSnapshot:
        """Atomically capture a late-attach boundary and ensure sufficient capacity."""
        with self._state_lock:
            sample_index = self._emitted_sample_index
            chunk_index = self._emitted_chunk_index
            total_duration = (sample_index / self._sample_rate) + float(record_duration)
            self._ensure_duration_locked(total_duration)
            return RecorderAttachSnapshot(
                chunk_index=chunk_index,
                sample_index=sample_index,
            )

    def request_capture(self, record_duration: float) -> RecorderAttachment:
        """Atomically register a pending sample-exact capture owner.

        A request is either installed under the recorder state lock or rejected
        after terminal completion, stop, or an error has closed acceptance.
        Its exact sample boundary is resolved by a later callback holding the
        same lock.
        """

        warmup_samples = frames_for_duration(
            self._sample_rate,
            SESSION_WARMUP_SECONDS,
        )
        target_samples = frames_for_duration(
            self._sample_rate,
            record_duration,
        )
        with self._state_lock:
            if not self._accepting_captures:
                raise CaptureNotAcceptedError(
                    "audio recorder is no longer accepting capture requests"
                )
            request_monotonic = float(self._monotonic_func())
            owner = RecorderAttachment(
                request_monotonic=request_monotonic,
                warmup_samples=warmup_samples,
                target_samples=target_samples,
            )
            self._pending_capture_owners[owner] = owner
            return owner

    def release_capture(self, owner: RecorderAttachment):
        """Release one pending or resolved capacity owner idempotently."""

        if not isinstance(owner, RecorderAttachment):
            raise TypeError("owner must be RecorderAttachment")
        with self._state_lock:
            known_owner = (
                self._pending_capture_owners.get(owner) is owner
                or self._active_capture_owners.get(owner) is owner
            )
            if not known_owner and not owner.released:
                raise ValueError("capture owner does not belong to this recorder")
            self._pending_capture_owners.pop(owner, None)
            self._active_capture_owners.pop(owner, None)
            owner._mark_released()

    def get_capture_buffer(
        self,
        owner: RecorderAttachment,
        *,
        retained_frames: int,
    ) -> np.ndarray:
        """Copy an exact retained prefix for a currently active owner.

        Pending and released handles have no stable extractable range and
        return an empty channel-shaped copy.  Requested frames are capped by
        both the owner's target and the absolute data written so far.
        """

        if not isinstance(owner, RecorderAttachment):
            raise TypeError("owner must be RecorderAttachment")
        if isinstance(retained_frames, (bool, np.bool_)):
            raise TypeError("retained_frames must be a non-boolean integer")
        try:
            retained_frames = int(operator.index(retained_frames))
        except TypeError as exc:
            raise TypeError(
                "retained_frames must be a non-boolean integer"
            ) from exc
        if retained_frames < 0:
            raise ValueError("retained_frames must be non-negative")

        with self._state_lock:
            empty = np.empty((0, self._n_channels), dtype=np.float32)
            if owner.released:
                return empty
            if self._active_capture_owners.get(owner) is not owner:
                return empty
            bounds = owner.resolution
            if bounds is None:
                return empty

            requested_end = bounds.retained_start + min(
                retained_frames,
                bounds.target_samples,
            )
            available_end = min(requested_end, bounds.retained_end, self._write_pos)
            if available_end <= bounds.retained_start:
                return empty
            return self._buffer[bounds.retained_start : available_end].copy()

    def get_buffer_from(self, snapshot: RecorderAttachSnapshot | None) -> np.ndarray:
        """Return audio written at or after a recorder-owned attach snapshot."""
        with self._state_lock:
            start_index = 0 if snapshot is None else int(snapshot.sample_index)
            legacy_end = min(self._write_pos, self._legacy_target_samples)
            start = max(0, min(start_index, legacy_end))
            return self._buffer[start:legacy_end].copy()

    def _ensure_duration_locked(self, record_duration: float):
        if record_duration <= self._record_duration:
            return

        new_total = int(self._sample_rate * record_duration)
        self._ensure_capacity_locked(new_total)
        self._legacy_target_samples = new_total
        self._record_duration = record_duration

    def _ensure_capacity_locked(self, required_end: int):
        """Grow raw storage without changing any existing absolute sample."""

        if required_end <= self._buffer.shape[0]:
            return
        new_buffer = np.zeros((required_end, self._n_channels), dtype=np.float32)
        new_buffer[: self._write_pos] = self._buffer[: self._write_pos]
        self._buffer = new_buffer

    def run(self):
        """Open sounddevice stream and drain chunks until done."""
        try:
            device_index = _resolve_device_index(self._device_name, self._hostapi)
        except ValueError as exc:
            self._close_capture_acceptance()
            self.error_occurred.emit(str(exc))
            return

        completed = False
        try:
            with sd.InputStream(
                device=device_index,
                samplerate=self._sample_rate,
                channels=self._n_channels,
                dtype="float32",
                blocksize=self._chunk_samples,
                callback=self._sd_callback,
            ):
                while True:
                    with self._state_lock:
                        if self._stop_flag:
                            break
                        if self._commit_capture_completion_locked():
                            completed = True
                            break
                    try:
                        block = self._queue.get(timeout=0.1)
                        self._feed_captured_block(block)
                        self._emit_captured_block(block)
                    except queue.Empty:
                        continue
        except sd.PortAudioError as exc:
            self._close_capture_acceptance()
            self.error_occurred.emit(f"Audio device error: {exc}")
            return
        except Exception as exc:
            self._close_capture_acceptance()
            self.error_occurred.emit(f"Recording error: {exc}")
            return

        if completed:
            self.recording_done.emit()

    def stop(self):
        """Signal the recording thread to stop."""
        with self._state_lock:
            self._stop_flag = True
            self._accepting_captures = False

    def get_buffer(self) -> np.ndarray:
        """Return the recorded audio (only the written portion)."""
        with self._state_lock:
            legacy_end = min(self._write_pos, self._legacy_target_samples)
            return self._buffer[:legacy_end].copy()

    def _sd_callback(self, indata, frames, time_info, status):
        """sounddevice callback. Runs on the PortAudio thread."""
        callback_monotonic = float(self._monotonic_func())
        if status:
            self.error_occurred.emit(f"PortAudio status: {status}")

        data = indata.copy()
        frame_count = data.shape[0]
        monotonic_span = self._map_callback_time(
            time_info=time_info,
            callback_monotonic=callback_monotonic,
            frame_count=frame_count,
        )
        should_log_degradation = False
        with self._state_lock:
            if not self._accepting_captures:
                return
            sample_start = self._capture_sample_cursor
            sample_end = sample_start + frame_count
            self._capture_sample_cursor = sample_end
            if monotonic_span is None:
                monotonic_start = None
                monotonic_end = None
                if not self._timing_degradation_logged:
                    self._timing_degradation_logged = True
                    should_log_degradation = True
            else:
                monotonic_start, monotonic_end = monotonic_span

            block = CapturedAudioBlock(
                data=data,
                sample_start=sample_start,
                sample_end=sample_end,
                monotonic_start=monotonic_start,
                monotonic_end=monotonic_end,
            )
            self._resolve_pending_capture_owners_locked(
                block,
                callback_monotonic=callback_monotonic,
            )
            self._queue.put(block)

        if should_log_degradation:
            logger.warning(
                "PortAudio ADC time is unavailable; excluding the whole "
                "current callback block for capture attachment"
            )

    def _map_callback_time(
        self,
        *,
        time_info,
        callback_monotonic: float,
        frame_count: int,
    ) -> tuple[float, float] | None:
        """Map PortAudio stream time to monotonic time when all inputs are valid."""

        try:
            adc_time = float(time_info.inputBufferAdcTime)
            current_time = float(time_info.currentTime)
            if not all(
                math.isfinite(value)
                for value in (adc_time, current_time, callback_monotonic)
            ):
                return None
            block_start = callback_monotonic + (adc_time - current_time)
            block_end = block_start + frame_count / self._sample_rate
        # PortAudio wrappers may surface backend-specific exceptions while
        # reading these fields.  Any such failure makes the timing unusable;
        # it must not escape the real-time callback or trigger time inference.
        except Exception:
            return None
        if not math.isfinite(block_start) or not math.isfinite(block_end):
            return None
        return block_start, block_end

    def _resolve_pending_capture_owners_locked(
        self,
        block: CapturedAudioBlock,
        *,
        callback_monotonic: float,
    ):
        """Resolve owners registered before this callback acquired the lock."""

        for owner_key, owner in list(self._pending_capture_owners.items()):
            if block.monotonic_start is None or block.monotonic_end is None:
                attach_sample = block.sample_end
                attach_monotonic = max(
                    owner.request_monotonic,
                    callback_monotonic,
                )
            else:
                if owner.request_monotonic > block.monotonic_end:
                    continue
                offset = math.ceil(
                    (owner.request_monotonic - block.monotonic_start)
                    * self._sample_rate
                )
                offset = min(max(offset, 0), block.sample_end - block.sample_start)
                attach_sample = block.sample_start + offset
                attach_monotonic = (
                    block.monotonic_start + offset / self._sample_rate
                )

            bounds = ResolvedCaptureBounds(
                attach_sample=attach_sample,
                attach_monotonic=attach_monotonic,
                warmup_samples=owner.warmup_samples,
                target_samples=owner.target_samples,
            )
            self._ensure_capacity_locked(bounds.retained_end)
            owner._resolve_once(bounds)
            self._pending_capture_owners.pop(owner_key)
            self._active_capture_owners[owner_key] = owner

    def _feed_captured_block(self, block: CapturedAudioBlock):
        """Write one callback-owned block at its exact absolute span."""

        if not isinstance(block, CapturedAudioBlock):
            raise TypeError("block must be CapturedAudioBlock")
        with self._state_lock:
            if block.sample_start != self._write_pos:
                raise ValueError(
                    "captured block is not adjacent to the written sample range: "
                    f"expected {self._write_pos}, got {block.sample_start}"
                )
            self._ensure_capacity_locked(block.sample_end)
            self._buffer[block.sample_start : block.sample_end] = block.data
            self._write_pos = block.sample_end

    def _emit_captured_block(self, block: CapturedAudioBlock):
        """Emit the same exact captured block plus the temporary legacy signal."""

        if not isinstance(block, CapturedAudioBlock):
            raise TypeError("block must be CapturedAudioBlock")
        with self._state_lock:
            chunk_index = self._chunk_index
            self._chunk_index += 1
            self._emitted_chunk_index = self._chunk_index
            self._emitted_sample_index = block.sample_end
        legacy_data = block.data.copy()
        self.captured_block_ready.emit(block)
        self.chunk_ready.emit(legacy_data, chunk_index)

    def _feed_chunk(self, data: np.ndarray):
        """Write chunk to ring buffer."""
        with self._state_lock:
            n_samples = data.shape[0]
            space = self._legacy_target_samples - self._write_pos
            if space <= 0:
                return
            n_write = min(n_samples, space)
            self._buffer[self._write_pos : self._write_pos + n_write] = data[:n_write]
            self._write_pos += n_write

    def _emit_chunk(self):
        """Emit the latest chunk via signal."""
        with self._state_lock:
            start = max(0, self._write_pos - self._chunk_samples)
            end = self._write_pos
            chunk = self._buffer[start:end].copy()
            chunk_index = self._chunk_index
            self._chunk_index += 1
            self._emitted_chunk_index = self._chunk_index
            self._emitted_sample_index = end
        self.chunk_ready.emit(chunk, chunk_index)

    def _is_buffer_full(self) -> bool:
        return self._capture_complete()

    def _legacy_buffer_full_locked(self) -> bool:
        return self._write_pos >= self._legacy_target_samples

    def _capture_complete_locked(self) -> bool:
        if self._pending_capture_owners:
            return False
        if self._active_capture_owners:
            required_end = max(
                owner.resolution.retained_end
                for owner in self._active_capture_owners.values()
                if owner.resolution is not None
            )
            return self._write_pos >= required_end
        return self._legacy_buffer_full_locked()

    def _capture_complete(self) -> bool:
        with self._state_lock:
            return self._capture_complete_locked()

    def _commit_capture_completion_locked(self) -> bool:
        """Atomically close capture acceptance if completion is now eligible."""

        if not self._accepting_captures:
            return False
        if not self._capture_complete_locked():
            return False
        self._accepting_captures = False
        return True

    def _commit_capture_completion(self) -> bool:
        with self._state_lock:
            return self._commit_capture_completion_locked()

    def _close_capture_acceptance(self):
        with self._state_lock:
            self._accepting_captures = False

    @property
    def elapsed_seconds(self) -> float:
        with self._state_lock:
            legacy_end = min(self._write_pos, self._legacy_target_samples)
            return legacy_end / self._sample_rate
