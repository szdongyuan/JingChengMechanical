"""Reusable, cancel-aware attachment preparation for run exports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from io import BytesIO
import math
from pathlib import Path
import wave

_AUDIO_FRAME_CHUNK = 4096


def _check(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check is not None:
        cancel_check()


def split_pcm16_wav(
    source_wav_path: str,
    *,
    sample_rate_hz: int,
    channel_count: int,
    frame_count: int,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[bytes, ...]:
    """Read a validated PCM16 WAV and return one mono WAV per input position."""
    if sample_rate_hz <= 0 or channel_count <= 0 or frame_count <= 0:
        raise ValueError("audio sampling facts must be positive")
    mono_payloads = tuple(bytearray() for _ in range(channel_count))
    try:
        _check(cancel_check)
        with wave.open(source_wav_path, "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise ValueError("source WAV compression is not PCM")
            if reader.getsampwidth() != 2:
                raise ValueError("source WAV is not PCM16")
            if reader.getframerate() != sample_rate_hz:
                raise ValueError("source WAV sample rate does not match")
            if reader.getnchannels() != channel_count:
                raise ValueError("source WAV channel count does not match")
            if reader.getnframes() != frame_count:
                raise ValueError("source WAV frame count does not match")
            remaining = frame_count
            while remaining:
                count = min(_AUDIO_FRAME_CHUNK, remaining)
                _check(cancel_check)
                payload = reader.readframes(count)
                _check(cancel_check)
                if len(payload) != count * channel_count * 2:
                    raise ValueError("source WAV PCM payload is truncated")
                frame_width = channel_count * 2
                for offset in range(0, len(payload), frame_width):
                    for channel_index, mono in enumerate(mono_payloads):
                        start = offset + channel_index * 2
                        mono.extend(payload[start : start + 2])
                remaining -= count
            _check(cancel_check)
            extra = reader.readframes(1)
            _check(cancel_check)
            if extra:
                raise ValueError("source WAV contains undeclared frames")
    except (EOFError, wave.Error, OSError) as exc:
        raise ValueError(f"invalid source WAV: {exc}") from None

    result = []
    for mono_payload in mono_payloads:
        _check(cancel_check)
        output = BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate_hz)
            for offset in range(0, len(mono_payload), _AUDIO_FRAME_CHUNK * 2):
                _check(cancel_check)
                writer.writeframesraw(
                    mono_payload[offset : offset + _AUDIO_FRAME_CHUNK * 2]
                )
                _check(cancel_check)
        _check(cancel_check)
        result.append(output.getvalue())
    return tuple(result)


def write_pcm16_wav_channels(
    source_wav_path: str,
    *,
    output_paths: tuple[Path, ...],
    sample_rate_hz: int,
    channel_count: int,
    frame_count: int,
    cancel_check: Callable[[], None] | None = None,
) -> None:
    """Stream one PCM16 source into bounded-memory mono WAV files."""
    if sample_rate_hz <= 0 or channel_count <= 0 or frame_count <= 0:
        raise ValueError("audio sampling facts must be positive")
    if type(output_paths) is not tuple or len(output_paths) != channel_count:
        raise ValueError("audio output path count does not match channel count")
    paths = tuple(Path(path) for path in output_paths)
    if len(set(paths)) != len(paths):
        raise ValueError("audio output paths must be unique")

    try:
        _check(cancel_check)
        with wave.open(source_wav_path, "rb") as reader, ExitStack() as stack:
            if reader.getcomptype() != "NONE":
                raise ValueError("source WAV compression is not PCM")
            if reader.getsampwidth() != 2:
                raise ValueError("source WAV is not PCM16")
            if reader.getframerate() != sample_rate_hz:
                raise ValueError("source WAV sample rate does not match")
            if reader.getnchannels() != channel_count:
                raise ValueError("source WAV channel count does not match")
            if reader.getnframes() != frame_count:
                raise ValueError("source WAV frame count does not match")

            writers = tuple(
                stack.enter_context(wave.open(str(path), "wb"))
                for path in paths
            )
            for writer in writers:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(sample_rate_hz)

            remaining = frame_count
            frame_width = channel_count * 2
            while remaining:
                count = min(_AUDIO_FRAME_CHUNK, remaining)
                _check(cancel_check)
                payload = reader.readframes(count)
                _check(cancel_check)
                if len(payload) != count * frame_width:
                    raise ValueError("source WAV PCM payload is truncated")
                mono_chunks = tuple(bytearray(count * 2) for _ in writers)
                for frame_index in range(count):
                    source_offset = frame_index * frame_width
                    target_offset = frame_index * 2
                    for channel_index, mono in enumerate(mono_chunks):
                        sample_offset = source_offset + channel_index * 2
                        mono[target_offset : target_offset + 2] = payload[
                            sample_offset : sample_offset + 2
                        ]
                for writer, mono in zip(writers, mono_chunks, strict=True):
                    writer.writeframesraw(mono)
                    _check(cancel_check)
                remaining -= count

            _check(cancel_check)
            extra = reader.readframes(1)
            _check(cancel_check)
            if extra:
                raise ValueError("source WAV contains undeclared frames")
    except (EOFError, wave.Error, OSError) as exc:
        raise ValueError(f"invalid source WAV: {exc}") from None


def serialize_current_csv(samples: tuple[tuple[float, float], ...]) -> bytes:
    """Validate a complete current trace and serialize its exact CSV."""
    if type(samples) is not tuple or not samples:
        raise ValueError("current samples must be a nonempty tuple")
    rows = ["time_sec,current_a"]
    previous_time: float | None = None
    for sample in samples:
        if type(sample) is not tuple or len(sample) != 2:
            raise ValueError("current sample must contain exactly two values")
        time_sec, current_a = sample
        if (
            isinstance(time_sec, bool)
            or isinstance(current_a, bool)
            or not isinstance(time_sec, (int, float))
            or not isinstance(current_a, (int, float))
            or not math.isfinite(float(time_sec))
            or not math.isfinite(float(current_a))
            or time_sec < 0
            or (previous_time is not None and time_sec <= previous_time)
        ):
            raise ValueError("current samples are not finite and strictly ordered")
        rows.append(f"{time_sec},{current_a}")
        previous_time = float(time_sec)
    return ("\n".join(rows) + "\n").encode("utf-8")
