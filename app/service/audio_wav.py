"""Shared uncompressed RIFF/WAVE metadata and precision-preserving decoding.

Probing reads headers only. Decoding is worker work and returns normalized
float64 frames by original file column; floating-point samples are not scaled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

import numpy as np
from scipy.io import wavfile


@dataclass(frozen=True)
class WavMetadata:
    path: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    container_format: str
    bits_per_sample: int
    valid_bits_per_sample: int
    byte_size: int

    @property
    def sample_width_bytes(self) -> int:
        return self.bits_per_sample // 8

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate_hz


@dataclass(frozen=True)
class DecodedWav:
    metadata: WavMetadata
    samples: np.ndarray


def _numeric_format(fmt: bytes) -> tuple[int, int, int, int, int]:
    if len(fmt) < 16:
        raise ValueError("WAV fmt chunk is truncated")
    tag, channels, rate, _byte_rate, align, bits = struct.unpack_from('<HHIIHH', fmt)
    valid_bits = bits
    if tag == 65534:
        if len(fmt) < 40:
            raise ValueError("WAV extensible format is truncated")
        extra_size = struct.unpack_from('<H', fmt, 16)[0]
        if extra_size < 22 or len(fmt) < 18 + extra_size:
            raise ValueError("WAV extensible format is truncated")
        valid_bits, _mask, guid = struct.unpack_from('<HI16s', fmt, 18)
        tag = struct.unpack_from('<I', guid)[0]
        if guid[4:] != bytes.fromhex('00001000800000aa00389b71'):
            raise ValueError("WAV extensible subtype is unsupported; use PCM or IEEE float")
    if (tag == 1 and bits not in {8, 16, 24, 32, 64}) or (
        tag == 3 and (bits not in {32, 64} or valid_bits != bits)
    ) or tag not in {1, 3} or not 0 < valid_bits <= bits:
        raise ValueError("WAV sample format is unsupported; use PCM 8/16/24/32/64 or IEEE float 32/64")
    if channels <= 0 or rate <= 0:
        raise ValueError("WAV sample rate and channel count must be positive")
    if align != channels * (bits // 8):
        raise ValueError("WAV frame alignment does not match its sample format")
    return tag, channels, rate, bits, valid_bits


def probe_wav(path: str | Path) -> WavMetadata:
    """Read format/frame facts without loading samples; reject incomplete data."""
    source = Path(path).resolve(strict=False)
    try:
        byte_size = source.stat().st_size
        with source.open('rb') as handle:
            header = handle.read(12)
            if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
                raise ValueError("Invalid WAV RIFF/WAVE header")
            riff_end = 8 + struct.unpack_from('<I', header, 4)[0]
            if riff_end > byte_size:
                raise ValueError("WAV RIFF payload is truncated")
            numeric = None
            data_size = None
            while handle.tell() + 8 <= riff_end:
                name, size = struct.unpack('<4sI', handle.read(8))
                end = handle.tell() + size
                if end > riff_end:
                    raise ValueError("WAV chunk payload is truncated")
                if name == b'fmt ':
                    numeric = _numeric_format(handle.read(min(size, 65553)))
                elif name == b'data':
                    data_size = size
                handle.seek(end + (size % 2))
            if numeric is None or data_size is None:
                raise ValueError("WAV requires fmt and data chunks")
            tag, channels, rate, bits, valid_bits = numeric
            align = channels * (bits // 8)
            if data_size % align:
                raise ValueError("WAV data ends in a partial frame")
            frames = data_size // align
            if frames <= 0:
                raise ValueError("WAV contains no frames")
    except OSError as exc:
        raise ValueError(f"WAV could not be read: {exc}") from exc
    return WavMetadata(str(source), rate, channels, frames,
                       'WAV_PCM' if tag == 1 else 'WAV_IEEE_FLOAT',
                       bits, valid_bits, byte_size)


def decode_wav(path: str | Path) -> DecodedWav:
    """Decode full original columns to finite float64 with source-appropriate scale.

    SciPy left-justifies packed PCM24 in int32. Scaling by the decoded integer
    dtype therefore retains its low bits (also for extensible valid-bit PCM).
    This helper does not change the legacy captured-PCM16 analysis scale.
    """
    metadata = probe_wav(path)
    try:
        rate, data = wavfile.read(metadata.path)
    except Exception as exc:
        raise ValueError(f"WAV samples could not be decoded: {exc}") from exc
    data = np.asarray(data).reshape(-1, metadata.channel_count)
    if rate != metadata.sample_rate_hz or data.shape[0] != metadata.frame_count:
        raise ValueError("Decoded WAV frames do not match its header")
    samples = data.astype(np.float64)
    if np.issubdtype(data.dtype, np.signedinteger):
        samples /= float(2 ** (np.iinfo(data.dtype).bits - 1))
    elif data.dtype == np.dtype('uint8'):
        samples = (samples - 128.0) / 128.0
    elif not np.issubdtype(data.dtype, np.floating):
        raise ValueError("Decoded WAV sample format is unsupported")
    if not np.all(np.isfinite(samples)):
        raise ValueError("WAV samples contain nonfinite values")
    return DecodedWav(metadata, samples)
