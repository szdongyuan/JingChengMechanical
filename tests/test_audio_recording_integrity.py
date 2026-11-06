"""Sample-level checks from a reused device buffer through the saved WAV.

The device is simulated; these tests cannot establish WASAPI/driver integrity.
"""
from __future__ import annotations

from itertools import cycle
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.test_session import TestSession


@pytest.mark.parametrize("selected_channels", [(0, 2), (1, 3)])
@pytest.mark.parametrize("frame_sizes", [(2400,), (100, 480, 960, 128, 2400, 37, 1024)])
@pytest.mark.parametrize("drain_during_capture", [False, True])
def test_48khz_nonadjacent_channels_preserve_every_sample_in_wav(
    monkeypatch, tmp_path, selected_channels, frame_sizes, drain_during_capture
):
    sample_rate = 48000
    duration = 0.2
    warmup_frames = 2400
    retained_frames = 9600
    total_frames = warmup_frames + retained_frames
    # Unique, exactly representable values identify both frame and channel.
    source = np.arange(total_frames * 4, dtype=np.float32).reshape(-1, 4)
    source = source / np.float32(2**18) - np.float32(0.5)
    clock = SimpleNamespace(now=99.0)
    recorder = AudioRecorder(
        device_name="Simulated four-channel input",
        hostapi="Windows WASAPI",
        sample_rate=sample_rate,
        n_channels=4,
        record_duration=duration,
        monotonic_func=lambda: clock.now,
    )
    session = TestSession(
        config={
            "config_id": 1,
            "config_name": "Recording integrity",
            "input_device_samplerate": sample_rate,
            "record_duration": duration,
            "selected_channels": list(selected_channels),
        },
        bus=MagicMock(),
        ai_service=MagicMock(),
        mic_cal={},
        workorder={},
        audio_dir=str(tmp_path),
    )
    token = object()
    session.prepare_for_attach(token)
    owner = recorder.request_capture(record_duration=duration)
    session.attach_to_recorder(owner, source_token=token)
    recorder.captured_block_ready.connect(
        lambda block: session._on_captured_block(block, source_token=token)
    )
    errors = []
    done = []
    recorder.error_occurred.connect(errors.append)
    recorder.recording_done.connect(lambda: done.append(True))
    monitor_chunks = []
    display_chunks = []
    session.set_monitor_feed(lambda data: monitor_chunks.append(data.copy()))
    session._live_display_dispatcher = SimpleNamespace(
        submit=lambda data, **kwargs: display_chunks.append(data.copy())
    )

    class SimulatedInputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            assert kwargs["samplerate"] == sample_rate
            assert kwargs["channels"] == 4

        def __enter__(self):
            # Reuse and overwrite device memory immediately after each callback.
            # In the delayed case all callbacks finish before the worker drains.
            device_buffer = np.empty((max(frame_sizes), 4), dtype=np.float32)
            position = 0
            for size in cycle(frame_sizes):
                size = min(size, total_frames - position)
                device_buffer[:size] = source[position : position + size]
                clock.now = 100.0 + (position + size) / sample_rate
                self.callback(
                    device_buffer[:size],
                    size,
                    SimpleNamespace(
                        inputBufferAdcTime=10.0 + position / sample_rate,
                        currentTime=10.0 + (position + size) / sample_rate,
                    ),
                    None,
                )
                device_buffer.fill(np.nan)
                if drain_during_capture:
                    block = recorder._queue.get_nowait()
                    recorder._feed_captured_block(block)
                    recorder._emit_captured_block(block)
                position += size
                if position == total_frames:
                    break
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        "app.service.analysis.audio_recorder._resolve_device_index", lambda *_: 0
    )
    monkeypatch.setattr(
        "app.service.analysis.audio_recorder.sd.InputStream", SimulatedInputStream
    )
    recorder.run()

    assert errors == []
    assert done == [True]
    assert session.freeze_capture() == retained_frames
    retained_source = source[warmup_frames:]
    np.testing.assert_array_equal(np.concatenate(monitor_chunks), retained_source)
    np.testing.assert_array_equal(np.concatenate(display_chunks), retained_source)
    captured = recorder.get_capture_buffer(owner, retained_frames=retained_frames)
    session.capture_recorder_buffer(captured, sample_rate)
    path = session._save_wav(session.buffer, sample_rate)
    saved_rate, saved = wavfile.read(path)
    assert saved_rate == sample_rate
    assert saved.shape == (retained_frames, 2)
    np.testing.assert_array_equal(saved, retained_source[:, selected_channels])
