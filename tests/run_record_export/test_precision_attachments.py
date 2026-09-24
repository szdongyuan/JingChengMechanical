"""Export mono attachments preserve raw source samples and format, without DSP."""
import struct

import numpy as np
import pytest

from app.service.audio_wav import probe_wav, decode_wav
from tests.test_audio_wav import write_numeric_wav


def data_bytes(path):
    raw = path.read_bytes()
    offset = raw.index(b'data')
    size = struct.unpack_from('<I', raw, offset + 4)[0]
    return raw[offset + 8:offset + 8 + size]


@pytest.mark.parametrize('bits,floating', [(8, False), (16, False), (24, False),
    (32, False), (64, False), (32, True), (64, True)])
@pytest.mark.parametrize('extensible', [False, True])
def test_export_preserves_every_sample_byte_and_source_format(tmp_path, bits, floating, extensible):
    from app.service.run_record_export.attachments import write_wav_channels
    source = tmp_path / 'source.wav'
    expected = write_numeric_wav(source, bits=bits, floating=floating, extensible=extensible)
    source_bytes = source.read_bytes()
    outputs = (tmp_path / 'left.wav', tmp_path / 'right.wav')
    write_wav_channels(str(source), output_paths=outputs, sample_rate_hz=8000, channel_count=2, frame_count=3)
    sample_bytes = np.frombuffer(data_bytes(source), dtype=np.uint8).reshape(3, 2, bits // 8)
    for column, output in enumerate(outputs):
        meta = probe_wav(output)
        assert (meta.bits_per_sample, meta.valid_bits_per_sample, meta.channel_count) == (bits, bits, 1)
        assert meta.container_format == ('WAV_IEEE_FLOAT' if floating else 'WAV_PCM')
        assert data_bytes(output) == sample_bytes[:, column, :].tobytes()
        np.testing.assert_array_equal(decode_wav(output).samples[:, 0], expected[:, column])
    assert source.read_bytes() == source_bytes


def test_export_extensible_valid_bits_and_streamed_cancellation(tmp_path):
    from app.service.run_record_export.attachments import write_wav_channels
    source = tmp_path / 'source.wav'
    values = np.column_stack((np.arange(9000) * 256, -np.arange(9000) * 256))
    write_numeric_wav(source, bits=32, extensible=True, valid_bits=24, values=values)
    outputs = (tmp_path / 'left.wav', tmp_path / 'right.wav')
    checks = []
    write_wav_channels(str(source), output_paths=outputs, sample_rate_hz=8000, channel_count=2,
        frame_count=9000, cancel_check=lambda: checks.append(True))
    assert len(checks) >= 9
    assert all(probe_wav(path).valid_bits_per_sample == 24 for path in outputs)
    assert data_bytes(outputs[0]) == values[:, 0].astype('<i4').tobytes()
    calls = []
    def cancel():
        calls.append(True)
        if len(calls) == 5:
            raise InterruptedError('cancel export')
    with pytest.raises(InterruptedError, match='cancel export'):
        write_wav_channels(str(source), output_paths=outputs, sample_rate_hz=8000, channel_count=2,
            frame_count=9000, cancel_check=cancel)


def test_export_rejects_mismatched_sampling_facts_before_creating_files(tmp_path):
    from app.service.run_record_export.attachments import write_wav_channels
    source = tmp_path / 'source.wav'
    write_numeric_wav(source)
    outputs = (tmp_path / 'left.wav', tmp_path / 'right.wav')
    with pytest.raises(ValueError, match='match'):
        write_wav_channels(str(source), output_paths=outputs, sample_rate_hz=16000, channel_count=2, frame_count=3)
    assert not any(path.exists() for path in outputs)
