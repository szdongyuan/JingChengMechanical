"""Audio monitoring playback. Plays one channel to the output device.

Receives multi-channel audio chunks and plays a single selected channel
through the configured output device for real-time monitoring.
"""
from __future__ import annotations

import os
import time

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread, pyqtSignal

from app.service.analysis.audio_recorder import _resolve_device_index
from app.service.analysis.monitor_playback import (
    MONITOR_BUFFER_SECONDS,
    MONITOR_OUTPUT_LATENCY_SECONDS,
    MONITOR_PREBUFFER_SECONDS,
    MonitorPlaybackBuffer,
    apply_monitor_gain,
)

os.environ.setdefault("SD_ENABLE_ASIO", "1")


class AudioMonitor(QThread):
    """Play a single channel to the output device for monitoring."""

    error_occurred = pyqtSignal(str)
    _ERROR_THROTTLE_SECONDS = 1.0

    def __init__(
        self,
        device_name: str,
        hostapi: str,
        sample_rate: float,
        monitor_channel: int,
        gain_db: float = 0.0,
        prebuffer_seconds: float = MONITOR_PREBUFFER_SECONDS,
        buffer_seconds: float = MONITOR_BUFFER_SECONDS,
        output_latency_seconds: float = MONITOR_OUTPUT_LATENCY_SECONDS,
    ):
        super().__init__()
        self._device_name = device_name
        self._hostapi = hostapi
        self._sample_rate = sample_rate
        self._monitor_channel = monitor_channel
        self._gain_db = gain_db
        self._output_latency_seconds = output_latency_seconds
        self._playback_buffer = MonitorPlaybackBuffer(
            sample_rate=sample_rate,
            capacity_seconds=buffer_seconds,
            prebuffer_seconds=prebuffer_seconds,
        )
        self._last_error_emit: dict[str, float] = {}
        self._stop_flag = False

    def feed(self, chunk: np.ndarray) -> bool:
        """Feed a multi-channel chunk for playback without blocking indefinitely."""
        try:
            mono = self._extract_channel(chunk)
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return False
        samples = apply_monitor_gain(mono, self._gain_db)
        if not self._playback_buffer.append(samples):
            self._emit_monitor_error("Monitoring buffer overflow", "overflow")
            return False
        return True

    def run(self):
        """Open callback-mode output stream and keep it alive until stopped."""
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
                latency=self._output_latency_seconds,
                callback=self._output_callback,
            ):
                while not self._stop_flag:
                    time.sleep(0.02)
        except sd.PortAudioError as exc:
            self.error_occurred.emit(f"Audio device error: {exc}")
        except Exception as exc:
            self.error_occurred.emit(f"Monitoring error: {exc}")

    def stop(self):
        self._stop_flag = True

    def _output_callback(self, outdata, frames, time_info, status):
        """PortAudio output callback. Drains buffered mono frames without blocking."""
        if status:
            self._emit_monitor_error(
                f"Monitoring output status: {status}",
                f"status:{status}",
            )

        samples, underrun = self._playback_buffer.drain(frames)
        if outdata.ndim == 1:
            outdata[:frames] = samples[:, 0]
        else:
            outdata[:frames, 0:1] = samples
            if outdata.shape[1] > 1:
                outdata[:frames, 1:] = 0.0

        if underrun:
            self._emit_monitor_error("Monitoring output underrun", "underrun")

    def _extract_channel(self, chunk: np.ndarray) -> np.ndarray:
        """Extract the monitor channel from multi-channel data."""
        array = np.asarray(chunk, dtype=np.float32)
        if array.ndim == 1:
            if self._monitor_channel != 0:
                raise ValueError(
                    "Monitor channel "
                    f"{self._monitor_channel} outside available channel range 0..0"
                )
            return array.reshape(-1, 1).copy()
        if array.ndim != 2:
            raise ValueError("Monitor chunk must be a 1-D or 2-D array")
        if self._monitor_channel < 0 or self._monitor_channel >= array.shape[1]:
            raise ValueError(
                "Monitor channel "
                f"{self._monitor_channel} outside available channel range "
                f"0..{array.shape[1] - 1}"
            )
        return array[:, self._monitor_channel : self._monitor_channel + 1].copy()

    def _emit_monitor_error(self, message: str, key: str | None = None):
        now = time.monotonic()
        throttle_key = key or message
        last_emit = self._last_error_emit.get(throttle_key)
        if last_emit is not None and now - last_emit < self._ERROR_THROTTLE_SECONDS:
            return
        self._last_error_emit[throttle_key] = now
        self.error_occurred.emit(message)
