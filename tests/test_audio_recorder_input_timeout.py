from __future__ import annotations

import numpy as np

from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.session_capture import CapturedAudioBlock


class _SilentInputStream:
    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_silent_input_stream_emits_error_and_ends_recorder(monkeypatch):
    monkeypatch.setattr(
        "app.service.analysis.audio_recorder._resolve_device_index",
        lambda *_args: 7,
    )
    monkeypatch.setattr(
        "app.service.analysis.audio_recorder.sd.InputStream",
        _SilentInputStream,
    )
    recorder = AudioRecorder(
        device_name="Silent Mic",
        hostapi="ASIO",
        sample_rate=1000,
        n_channels=1,
        record_duration=1.0,
    )
    recorder.INPUT_DATA_TIMEOUT_SECONDS = 0.01
    errors = []
    completed = []
    recorder.error_occurred.connect(errors.append)
    recorder.recording_done.connect(lambda: completed.append(True))

    recorder.run()

    assert errors == [
        "输入硬件未返回音频数据，录音无法开始，请检查设备连接和占用状态。"
    ]
    assert completed == []


def test_input_stream_stall_after_first_block_emits_disconnect_error(monkeypatch):
    monkeypatch.setattr(
        "app.service.analysis.audio_recorder._resolve_device_index",
        lambda *_args: 7,
    )
    monkeypatch.setattr(
        "app.service.analysis.audio_recorder.sd.InputStream",
        _SilentInputStream,
    )
    recorder = AudioRecorder(
        device_name="Disconnected Mic",
        hostapi="ASIO",
        sample_rate=1000,
        n_channels=1,
        record_duration=1.0,
    )
    recorder.INPUT_DATA_TIMEOUT_SECONDS = 0.01
    recorder._queue.put(
        CapturedAudioBlock(
            data=np.zeros((10, 1), dtype=np.float32),
            sample_start=0,
            sample_end=10,
            monotonic_start=None,
            monotonic_end=None,
        )
    )
    errors = []
    recorder.error_occurred.connect(errors.append)

    recorder.run()

    assert errors == ["输入硬件已停止返回音频数据，请检查设备连接。"]
