"""Audio monitoring playback. Plays one channel to the output device.

Receives multi-channel audio chunks and plays a single selected channel
through the configured output device for real-time monitoring.
"""
from __future__ import annotations

import os
import queue

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread, pyqtSignal

from app.service.analysis.audio_recorder import _resolve_device_index

os.environ.setdefault("SD_ENABLE_ASIO", "1")


class AudioMonitor(QThread):
    """Play a single channel to the output device for monitoring."""

    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        device_name: str,
        hostapi: str,
        sample_rate: float,
        monitor_channel: int,
    ):
        super().__init__()
        self._device_name = device_name
        self._hostapi = hostapi
        self._sample_rate = sample_rate
        self._monitor_channel = monitor_channel
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._stop_flag = False

    def feed(self, chunk: np.ndarray):
        """Feed a multi-channel chunk for playback. Drops if the queue is full."""
        mono = self._extract_channel(chunk)
        try:
            self._queue.put_nowait(mono)
        except queue.Full:
            pass

    def run(self):
        """Open output stream and play queued chunks."""
        self._stop_flag = False
        try:
            device_index = _resolve_device_index(self._device_name, self._hostapi)
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return

        try:
            with sd.OutputStream(
                device=device_index,
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
            ) as stream:
                while not self._stop_flag:
                    try:
                        mono = self._queue.get(timeout=0.1)
                        stream.write(mono)
                    except queue.Empty:
                        continue
        except sd.PortAudioError as exc:
            self.error_occurred.emit(f"Audio device error: {exc}")
        except Exception as exc:
            self.error_occurred.emit(f"Monitoring error: {exc}")

    def stop(self):
        self._stop_flag = True

    def _extract_channel(self, chunk: np.ndarray) -> np.ndarray:
        """Extract the monitor channel from multi-channel data."""
        if chunk.ndim == 1:
            return chunk.reshape(-1, 1)
        if self._monitor_channel >= chunk.shape[1]:
            return np.zeros((chunk.shape[0], 1), dtype=np.float32)
        return chunk[:, self._monitor_channel : self._monitor_channel + 1].copy()
