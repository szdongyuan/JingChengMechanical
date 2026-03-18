"""Audio recording via sounddevice with ring buffer.

Runs a sounddevice InputStream in callback mode. A QThread drains chunks
from a queue and emits them as Qt signals for real-time analysis.
"""
from __future__ import annotations

import os
import queue

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

        self._queue: queue.Queue = queue.Queue()
        self._stop_flag = False

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
        return self._buffer[: self._write_pos].copy()

    def _sd_callback(self, indata, frames, time_info, status):
        """sounddevice callback. Runs on the PortAudio thread."""
        if status:
            self.error_occurred.emit(f"PortAudio status: {status}")
        self._queue.put(indata.copy())

    def _feed_chunk(self, data: np.ndarray):
        """Write chunk to ring buffer."""
        n_samples = data.shape[0]
        space = self._buffer.shape[0] - self._write_pos
        if space <= 0:
            return
        n_write = min(n_samples, space)
        self._buffer[self._write_pos : self._write_pos + n_write] = data[:n_write]
        self._write_pos += n_write

    def _emit_chunk(self):
        """Emit the latest chunk via signal."""
        start = max(0, self._write_pos - self._chunk_samples)
        chunk = self._buffer[start : self._write_pos]
        self.chunk_ready.emit(chunk, self._chunk_index)
        self._chunk_index += 1

    def _is_buffer_full(self) -> bool:
        return self._write_pos >= self._buffer.shape[0]

    @property
    def elapsed_seconds(self) -> float:
        return self._write_pos / self._sample_rate
