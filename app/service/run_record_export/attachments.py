"""Reusable, cancel-aware attachment preparation for run exports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from io import BytesIO
import math
from pathlib import Path
import struct
import wave

from app.service.audio_wav import probe_wav

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


def write_wav_channels(
    source_wav_path: str,
    *,
    output_paths: tuple[Path, ...],
    sample_rate_hz: int,
    channel_count: int,
    frame_count: int,
    cancel_check: Callable[[], None] | None = None,
) -> None:
    """Stream original sample bytes into mono WAVs, preserving format/valid bits.

    Shared probing owns format validation. The RIFF walk below only locates
    the original format and data chunks; it never converts sample values.
    """
    _check(cancel_check)
    metadata = probe_wav(source_wav_path)
    if (metadata.sample_rate_hz, metadata.channel_count, metadata.frame_count) != (
        sample_rate_hz, channel_count, frame_count
    ):
        raise ValueError("source WAV sampling facts do not match")
    if type(output_paths) is not tuple or len(output_paths) != channel_count:
        raise ValueError("audio output path count does not match channel count")
    paths = tuple(Path(path) for path in output_paths)
    if len(set(paths)) != len(paths):
        raise ValueError("audio output paths must be unique")
    with open(metadata.path, 'rb') as source:
        riff_end = 8 + struct.unpack_from('<I', source.read(12), 4)[0]
        fmt = None
        data_offset = None
        while source.tell() + 8 <= riff_end:
            _check(cancel_check)
            name, size = struct.unpack('<4sI', source.read(8))
            start = source.tell()
            if name == b'fmt ':
                fmt = bytearray(source.read(min(size, 65553)))
            elif name == b'data':
                data_offset = start
            source.seek(start + size + size % 2)
        if fmt is None or data_offset is None:
            raise ValueError("source WAV requires fmt and data chunks")
        tag = struct.unpack_from('<H', fmt)[0]
        if tag == 1 and metadata.bits_per_sample == 16:
            # Preserve the existing captured PCM16 writer and cancellation path.
            return write_pcm16_wav_channels(source_wav_path, output_paths=paths,
                sample_rate_hz=sample_rate_hz, channel_count=channel_count,
                frame_count=frame_count, cancel_check=cancel_check)
        width = metadata.sample_width_bytes
        struct.pack_into('<H', fmt, 2, 1)
        struct.pack_into('<I', fmt, 8, sample_rate_hz * width)
        struct.pack_into('<H', fmt, 12, width)
        if tag == 65534:
            struct.pack_into('<I', fmt, 20, 0)  # Mono column has no speaker assignment.
        fmt_chunk = b'fmt ' + struct.pack('<I', len(fmt)) + fmt + b'\0' * (len(fmt) % 2)
        fact_chunk = (b'fact' + struct.pack('<II', 4, frame_count)
            if metadata.container_format == 'WAV_IEEE_FLOAT' else b'')
        data_size = frame_count * width
        header = (b'RIFF' + struct.pack('<I', 4 + len(fmt_chunk) + len(fact_chunk) + 8 + data_size + data_size % 2)
            + b'WAVE' + fmt_chunk + fact_chunk + b'data' + struct.pack('<I', data_size))
        with ExitStack() as stack:
            writers = tuple(stack.enter_context(path.open('wb')) for path in paths)
            for writer in writers:
                writer.write(header)
            source.seek(data_offset)
            remaining = frame_count
            frame_width = channel_count * width
            while remaining:
                count = min(_AUDIO_FRAME_CHUNK, remaining)
                _check(cancel_check)
                payload = source.read(count * frame_width)
                _check(cancel_check)
                if len(payload) != count * frame_width:
                    raise ValueError("source WAV sample payload is truncated")
                for channel, writer in enumerate(writers):
                    mono = bytearray(count * width)
                    for frame in range(count):
                        start = frame * frame_width + channel * width
                        mono[frame * width:(frame + 1) * width] = payload[start:start + width]
                    writer.write(mono)
                    _check(cancel_check)
                remaining -= count
            if data_size % 2:
                for writer in writers:
                    writer.write(b'\0')


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
