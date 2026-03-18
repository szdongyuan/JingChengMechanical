"""Audio recording via sounddevice with ring buffer.

Runs a sounddevice InputStream in callback mode. A QThread drains chunks
from a queue and emits them as Qt signals for real-time analysis.
"""
from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread, pyqtSignal

os.environ.setdefault("SD_ENABLE_ASIO", "1")


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
    chunk_ready(ndarray, int)
        Emitted ~20 Hz with (chunk_data, chunk_index).
    recording_done()
        Emitted once the buffer is full.
    error_occurred(str)
        Emitted on device errors.
    """

    chunk_ready = pyqtSignal(object, int)
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
    ):
        super().__init__()
        self._device_name = device_name
        self._hostapi = hostapi
        self._sample_rate = sample_rate
        self._n_channels = n_channels
        self._record_duration = record_duration

        total_samples = int(sample_rate * record_duration)
        self._buffer = np.zeros((total_samples, n_channels), dtype=np.float32)
        self._write_pos = 0
        self._chunk_samples = int(sample_rate * self.CHUNK_DURATION)
        self._chunk_index = 0
        self._emitted_chunk_index = 0
        self._emitted_sample_index = 0
        self._state_lock = threading.Lock()

        self._queue: queue.Queue = queue.Queue()
        self._stop_flag = False

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

    def get_buffer_from(self, snapshot: RecorderAttachSnapshot | None) -> np.ndarray:
        """Return audio written at or after a recorder-owned attach snapshot."""
        with self._state_lock:
            start_index = 0 if snapshot is None else int(snapshot.sample_index)
            start = max(0, min(start_index, self._write_pos))
            return self._buffer[start : self._write_pos].copy()

    def _ensure_duration_locked(self, record_duration: float):
        if record_duration <= self._record_duration:
            return

        new_total = int(self._sample_rate * record_duration)
        if new_total <= self._buffer.shape[0]:
            self._record_duration = record_duration
            return

        new_buffer = np.zeros((new_total, self._n_channels), dtype=np.float32)
        new_buffer[: self._write_pos] = self._buffer[: self._write_pos]
        self._buffer = new_buffer
        self._record_duration = record_duration

    def run(self):
        """Open sounddevice stream and drain chunks until done."""
        self._stop_flag = False
        try:
            device_index = _resolve_device_index(self._device_name, self._hostapi)
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return

        try:
            with sd.InputStream(
                device=device_index,
                samplerate=self._sample_rate,
                channels=self._n_channels,
                dtype="float32",
                blocksize=self._chunk_samples,
                callback=self._sd_callback,
            ):
                while not self._stop_flag and not self._is_buffer_full():
                    try:
                        data = self._queue.get(timeout=0.1)
                        self._feed_chunk(data)
                        self._emit_chunk()
                    except queue.Empty:
                        continue
        except sd.PortAudioError as exc:
            self.error_occurred.emit(f"Audio device error: {exc}")
            return
        except Exception as exc:
            self.error_occurred.emit(f"Recording error: {exc}")
            return

        if self._is_buffer_full():
            self.recording_done.emit()

    def stop(self):
        """Signal the recording thread to stop."""
        self._stop_flag = True

    def get_buffer(self) -> np.ndarray:
        """Return the recorded audio (only the written portion)."""
        with self._state_lock:
            return self._buffer[: self._write_pos].copy()

    def _sd_callback(self, indata, frames, time_info, status):
        """sounddevice callback. Runs on the PortAudio thread."""
        if status:
            self.error_occurred.emit(f"PortAudio status: {status}")
        self._queue.put(indata.copy())

    def _feed_chunk(self, data: np.ndarray):
        """Write chunk to ring buffer."""
        with self._state_lock:
            n_samples = data.shape[0]
            space = self._buffer.shape[0] - self._write_pos
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
        with self._state_lock:
            return self._write_pos >= self._buffer.shape[0]

    @property
    def elapsed_seconds(self) -> float:
        with self._state_lock:
            return self._write_pos / self._sample_rate
