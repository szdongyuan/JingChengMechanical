"""Injected device-resolution and audio-input backend boundaries."""

from __future__ import annotations

import math
from typing import Callable, Protocol

from .contracts import ResolvedInputDevice


PRODUCTION_BLOCK_DURATION_SECONDS = 0.05


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


class InputDeviceResolver(Protocol):
    def resolve(
        self,
        hardware_id: str,
        desired_revision: int,
        resolution_attempt_id: int,
        cancel_token: CancelToken,
    ) -> ResolvedInputDevice: ...


class AudioInputHandle(Protocol):
    @property
    def input_latency(self) -> float: ...

    def start(self) -> None: ...

    def abort(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class AudioInputBackend(Protocol):
    def open_input(
        self,
        resolved: ResolvedInputDevice,
        *,
        block_frames: int,
        callback: Callable[..., None],
    ) -> AudioInputHandle: ...

    def force_terminate(self) -> None: ...


def production_block_frames(sample_rate: float) -> int:
    """Return the fixed 50 ms production callback request."""

    rate = float(sample_rate)
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("sample_rate must be positive and finite")
    return max(1, int(rate * PRODUCTION_BLOCK_DURATION_SECONDS))


class _SoundDeviceInputHandle:
    def __init__(self, stream: object) -> None:
        self._stream = stream

    @property
    def input_latency(self) -> float:
        value = getattr(self._stream, "latency", 0.0)
        try:
            latency = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return latency if math.isfinite(latency) and latency >= 0 else 0.0

    def start(self) -> None:
        self._stream.start()

    def abort(self) -> None:
        self._stream.abort()

    def stop(self) -> None:
        self._stream.stop()

    def close(self) -> None:
        self._stream.close()


class SoundDeviceAudioBackend:
    """Sounddevice adapter that never re-resolves a device by display name."""

    def __init__(self, sounddevice_module: object | None = None) -> None:
        if sounddevice_module is None:
            import sounddevice as sounddevice_module

        input_stream = getattr(sounddevice_module, "InputStream", None)
        if not callable(input_stream):
            raise TypeError("sounddevice module must provide InputStream")
        self._sounddevice = sounddevice_module

    def open_input(
        self,
        resolved: ResolvedInputDevice,
        *,
        block_frames: int,
        callback: Callable[..., None],
    ) -> AudioInputHandle:
        if not isinstance(resolved, ResolvedInputDevice):
            raise TypeError("resolved must be a ResolvedInputDevice")
        if isinstance(block_frames, bool) or not isinstance(block_frames, int):
            raise ValueError("block_frames must be a positive integer")
        if block_frames < 1:
            raise ValueError("block_frames must be a positive integer")
        if not callable(callback):
            raise TypeError("callback must be callable")
        stream = self._sounddevice.InputStream(
            device=resolved.device_index,
            samplerate=resolved.sample_rate,
            channels=resolved.channels,
            dtype=resolved.dtype,
            blocksize=block_frames,
            callback=callback,
        )
        return _SoundDeviceInputHandle(stream)

    def force_terminate(self) -> None:
        terminate = getattr(self._sounddevice, "_terminate", None)
        if callable(terminate):
            terminate()


__all__ = [
    "AudioInputBackend",
    "AudioInputHandle",
    "CancelToken",
    "InputDeviceResolver",
    "PRODUCTION_BLOCK_DURATION_SECONDS",
    "SoundDeviceAudioBackend",
    "production_block_frames",
]
