"""Monitor-only playback helpers."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from queue import Empty, SimpleQueue

import numpy as np
import sounddevice as sd

from app.service.analysis.audio_recorder import _resolve_device_index

MONITOR_PREBUFFER_SECONDS = 0.15
MONITOR_BUFFER_SECONDS = 10.0
MONITOR_OUTPUT_LATENCY_SECONDS = 0.05
MONITOR_GAIN_MIN_DB = -60.0
MONITOR_GAIN_MAX_DB = 35.0


def apply_monitor_gain(samples: np.ndarray, gain_db: float) -> np.ndarray:
    gain = 10 ** (float(gain_db) / 20.0)
    return np.clip(np.asarray(samples, dtype=np.float32) * gain, -1.0, 1.0)


class OneShotReplayPlayer:
    """Callback-mode player for one finite mono review segment."""

    _ERROR_THROTTLE_SECONDS = 1.0

    def __init__(
        self,
        device_resolver: Callable[[str, str], int] | None = None,
        output_latency_seconds: float = MONITOR_OUTPUT_LATENCY_SECONDS,
    ):
        self._device_resolver = device_resolver or _resolve_device_index
        self._output_latency_seconds = output_latency_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    def play(
        self,
        segment: np.ndarray,
        sample_rate: float,
        device_name: str,
        hostapi: str,
        gain_db: float = 0.0,
        error_callback: Callable[[str], None] | None = None,
    ) -> bool:
        self.stop(wait=True)

        try:
            samples = self._prepare_segment(segment, gain_db)
            resolved_sample_rate = float(sample_rate)
            if resolved_sample_rate <= 0:
                raise ValueError("Replay sample rate must be positive")
            device_index = self._device_resolver(device_name, hostapi)
        except ValueError as exc:
            self._emit_error(error_callback, str(exc))
            return False

        stop_event = threading.Event()
        startup_event = threading.Event()
        startup_started = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(
                samples,
                resolved_sample_rate,
                device_index,
                stop_event,
                startup_event,
                startup_started,
                error_callback,
            ),
            daemon=True,
            name="OneShotReplayPlayer",
        )
        with self._lock:
            self._thread = thread
            self._stop_event = stop_event
        thread.start()
        startup_event.wait()
        started = startup_started.is_set()
        if not started and thread is not threading.current_thread():
            thread.join()
        return started

    def stop(self, wait: bool = True) -> None:
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event

        if stop_event is not None:
            stop_event.set()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join()

        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
                self._stop_event = None

    def _run(
        self,
        samples: np.ndarray,
        sample_rate: float,
        device_index: int,
        stop_event: threading.Event,
        startup_event: threading.Event,
        startup_started: threading.Event,
        error_callback: Callable[[str], None] | None,
    ):
        done_event = threading.Event()
        pending_errors = SimpleQueue()
        callback = self._make_callback(samples, stop_event, done_event, pending_errors)
        try:
            with sd.OutputStream(
                device=device_index,
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                latency=self._output_latency_seconds,
                callback=callback,
            ):
                startup_started.set()
                startup_event.set()
                while not stop_event.is_set() and not done_event.is_set():
                    self._emit_pending_errors(error_callback, pending_errors)
                    time.sleep(0.02)
                self._emit_pending_errors(error_callback, pending_errors)
        except sd.PortAudioError as exc:
            self._emit_error(error_callback, f"Audio replay device error: {exc}")
        except Exception as exc:
            if not isinstance(exc, sd.CallbackStop):
                self._emit_error(error_callback, f"Audio replay error: {exc}")
        finally:
            startup_event.set()
            done_event.set()
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._stop_event = None

    def _make_callback(
        self,
        samples: np.ndarray,
        stop_event: threading.Event,
        done_event: threading.Event,
        pending_errors: SimpleQueue,
    ):
        position = 0
        total_frames = samples.shape[0]
        last_status_emit: dict[str, float] = {}

        def callback(outdata, frames, time_info, status):
            nonlocal position
            if status:
                message = f"Replay output status: {status}"
                key = f"status:{status}"
                now = time.monotonic()
                last_emit = last_status_emit.get(key)
                if last_emit is None or now - last_emit >= self._ERROR_THROTTLE_SECONDS:
                    last_status_emit[key] = now
                    pending_errors.put(message)

            requested = max(0, int(frames))
            if outdata.ndim == 1:
                outdata[:requested] = 0.0
            else:
                outdata[:requested, :] = 0.0

            remaining = total_frames - position
            n_write = min(requested, remaining)
            if n_write > 0:
                chunk = samples[position : position + n_write]
                if outdata.ndim == 1:
                    outdata[:n_write] = chunk[:, 0]
                else:
                    outdata[:n_write, 0:1] = chunk
                position += n_write

            if stop_event.is_set() or position >= total_frames:
                done_event.set()
                raise sd.CallbackStop

        return callback

    @staticmethod
    def _emit_pending_errors(
        error_callback: Callable[[str], None] | None,
        pending_errors: SimpleQueue,
    ) -> None:
        while True:
            try:
                message = pending_errors.get_nowait()
            except Empty:
                return
            OneShotReplayPlayer._emit_error(error_callback, message)

    @staticmethod
    def _prepare_segment(segment: np.ndarray, gain_db: float) -> np.ndarray:
        array = np.asarray(segment, dtype=np.float32)
        if array.ndim == 1:
            mono = array.reshape(-1, 1)
        elif array.ndim == 2 and array.shape[1] == 1:
            mono = array
        else:
            raise ValueError("Replay segment must be mono")
        if mono.shape[0] == 0:
            raise ValueError("Replay segment is empty")
        return apply_monitor_gain(mono, gain_db).reshape(-1, 1).astype(np.float32, copy=True)

    @staticmethod
    def _emit_error(
        error_callback: Callable[[str], None] | None,
        message: str,
    ):
        if error_callback is not None:
            error_callback(message)


class MonitorPlaybackBuffer:
    """Small frame-counted ring buffer for callback-mode monitor playback."""

    def __init__(
        self,
        sample_rate: float,
        capacity_seconds: float,
        prebuffer_seconds: float,
    ):
        self._capacity_frames = max(1, int(round(float(sample_rate) * float(capacity_seconds))))
        self._prebuffer_frames = max(
            0,
            min(
                self._capacity_frames,
                int(round(float(sample_rate) * float(prebuffer_seconds))),
            ),
        )
        self._data = np.zeros((self._capacity_frames, 1), dtype=np.float32)
        self._start = 0
        self._size = 0
        self._started = False
        self._lock = threading.Lock()

    def append(self, samples: np.ndarray) -> bool:
        """Append mono frames, dropping oldest buffered frames on overflow."""
        frames = self._as_mono(samples)
        n_frames = frames.shape[0]
        if n_frames == 0:
            return True

        with self._lock:
            overflowed = self._size + n_frames > self._capacity_frames
            if n_frames > self._capacity_frames:
                frames = frames[-self._capacity_frames :]
                n_frames = frames.shape[0]
                self._start = 0
                self._size = 0
                overflowed = True
            elif overflowed:
                drop = self._size + n_frames - self._capacity_frames
                self._drop_locked(drop)

            self._write_locked(frames)
            return not overflowed

    def buffered_frames(self) -> int:
        with self._lock:
            return self._size

    def drain(self, frames: int) -> tuple[np.ndarray, bool]:
        """Drain exactly ``frames`` frames, zero-filling prebuffer or underrun gaps."""
        requested = max(0, int(frames))
        out = np.zeros((requested, 1), dtype=np.float32)
        if requested == 0:
            return out, False

        with self._lock:
            if not self._started:
                if self._size < self._prebuffer_frames:
                    return out, False
                if self._prebuffer_frames == 0 and self._size == 0:
                    return out, False
                self._started = True

            n_read = min(requested, self._size)
            if n_read:
                self._read_locked(out, n_read)
            underrun = n_read < requested
            if underrun:
                self._started = False
            return out, underrun

    def _drop_locked(self, frames: int):
        n_drop = max(0, min(int(frames), self._size))
        if n_drop:
            self._start = (self._start + n_drop) % self._capacity_frames
            self._size -= n_drop

    def _write_locked(self, frames: np.ndarray):
        n_frames = frames.shape[0]
        write_at = (self._start + self._size) % self._capacity_frames
        first = min(n_frames, self._capacity_frames - write_at)
        self._data[write_at : write_at + first] = frames[:first]
        remaining = n_frames - first
        if remaining:
            self._data[:remaining] = frames[first:]
        self._size += n_frames

    def _read_locked(self, out: np.ndarray, frames: int):
        first = min(frames, self._capacity_frames - self._start)
        out[:first] = self._data[self._start : self._start + first]
        remaining = frames - first
        if remaining:
            out[first:frames] = self._data[:remaining]
        self._start = (self._start + frames) % self._capacity_frames
        self._size -= frames

    @staticmethod
    def _as_mono(samples: np.ndarray) -> np.ndarray:
        array = np.asarray(samples, dtype=np.float32)
        if array.ndim == 1:
            return array.reshape(-1, 1).copy()
        if array.ndim == 2 and array.shape[1] == 1:
            return array.copy()
        raise ValueError("Monitor playback buffer expects mono samples")
