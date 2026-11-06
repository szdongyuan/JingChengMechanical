from __future__ import annotations

import struct

import numpy as np
import pytest


def write_numeric_wav(path, *, bits=24, floating=False, extensible=False,
                      values=None, valid_bits=None):
    """Small real RIFF fixture, retaining packed PCM24 low bits and odd chunks."""
    if values is None:
        values = [[1, -1], [3, -3], [127, -127]]
        if floating:
            values = np.asarray(values) * 1e-8
        elif bits == 8:
            values = np.asarray(values) + 128
    values = np.asarray(values)
    channels = values.shape[1]
    tag = 3 if floating else 1
    align = channels * (bits // 8)
    fmt = struct.pack('<HHIIHH', 65534 if extensible else tag,
                      channels, 8000, 8000 * align, align, bits)
    if extensible:
        guid = struct.pack('<IHH8s', tag, 0, 16, bytes.fromhex('800000aa00389b71'))
        fmt += struct.pack('<HHI', 22, valid_bits or bits, 0) + guid
    if floating:
        payload = values.astype(f'<f{bits // 8}').tobytes()
    elif bits == 24:
        payload = b''.join(int(v).to_bytes(3, 'little', signed=True) for v in values.flat)
    else:
        payload = values.astype('u1' if bits == 8 else f'<i{bits // 8}').tobytes()
    def chunk(name, data):
        return name + struct.pack('<I', len(data)) + data + b'\0' * (len(data) % 2)
    body = b'WAVE' + chunk(b'JUNK', b'odd') + chunk(b'fmt ', fmt) + chunk(b'data', payload)
    path.write_bytes(b'RIFF' + struct.pack('<I', len(body)) + body)
    if floating:
        expected = values.astype(f'<f{bits // 8}').astype(np.float64)
    elif bits == 8:
        expected = (values.astype(np.float64) - 128) / 128
    else:
        expected = values.astype(np.float64) / 2 ** (bits - 1)
    return expected


@pytest.mark.parametrize('bits,floating', [(8, False), (16, False), (24, False),
                                         (32, False), (64, False), (32, True), (64, True)])
@pytest.mark.parametrize('extensible', [False, True])
def test_probe_and_decode_preserve_source_precision(tmp_path, bits, floating, extensible):
    from app.service.audio_wav import probe_wav, decode_wav

    path = tmp_path / 'precision.wav'
    expected = write_numeric_wav(path, bits=bits, floating=floating, extensible=extensible)
    metadata = probe_wav(path)
    decoded = decode_wav(path)

    assert decoded.metadata == metadata
    assert (metadata.sample_rate_hz, metadata.channel_count, metadata.frame_count) == (8000, 2, 3)
    assert metadata.sample_width_bytes == bits // 8
    assert metadata.bits_per_sample == metadata.valid_bits_per_sample == bits
    assert metadata.container_format == ('WAV_IEEE_FLOAT' if floating else 'WAV_PCM')
    assert metadata.duration_seconds == 3 / 8000
    assert decoded.samples.dtype == np.float64
    np.testing.assert_array_equal(decoded.samples, expected)


def test_extensible_24_valid_bits_in_32_bit_container(tmp_path):
    from app.service.audio_wav import probe_wav, decode_wav

    path = tmp_path / 'padded.wav'
    expected = write_numeric_wav(path, bits=32, extensible=True, valid_bits=24,
                                 values=[[256, -256], [768, -768]])
    assert probe_wav(path).valid_bits_per_sample == 24
    np.testing.assert_array_equal(decode_wav(path).samples, expected)


@pytest.mark.parametrize('fault,match', [('header', 'RIFF'), ('truncated', 'truncated'),
                                       ('empty', 'no frames'), ('unsupported', 'unsupported'),
                                       ('partial_frame', 'frame')])
def test_bad_wav_has_actionable_error(tmp_path, fault, match):
    from app.service.audio_wav import probe_wav, decode_wav

    path = tmp_path / 'bad.wav'
    write_numeric_wav(path, values=np.empty((0, 2)) if fault == 'empty' else None)
    raw = bytearray(path.read_bytes())
    if fault == 'header':
        raw[:4] = b'NOPE'
    elif fault == 'truncated':
        raw = raw[:-1]
    elif fault == 'unsupported':
        offset = raw.index(b'fmt ') + 8
        raw[offset:offset + 2] = struct.pack('<H', 6)
    elif fault == 'partial_frame':
        offset = raw.index(b'data') + 4
        raw[offset:offset + 4] = struct.pack('<I', 17)
    path.write_bytes(raw)
    for reader in (probe_wav, decode_wav):
        with pytest.raises(ValueError, match=match):
            reader(path)


@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_decoder_rejects_nonfinite_floating_audio(tmp_path, value):
    from app.service.audio_wav import decode_wav

    path = tmp_path / 'nonfinite.wav'
    write_numeric_wav(path, bits=64, floating=True, values=[[value]])
    with pytest.raises(ValueError, match='nonfinite'):
        decode_wav(path)
