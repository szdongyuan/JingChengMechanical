"""The external AI contract receives the complete, quantized saved capture file."""
from dataclasses import replace
import json
from types import SimpleNamespace
import wave

import numpy as np

from app.service.capture_process import adapter as bridge
from app.service.capture_process.protocol import WorkerConfig
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.scheduler import AnalysisExecutionStatus
from app.service.multi_run_coordinator_mvp.contracts import AudioSaveStatus, SaveCancelToken
from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
    ManagedResidentCapturePort, ManagedResidentCaptureRoute,
)
from app.service.run_persistence.prepare_database import prepare_run_persistence_database
from tests.capture_process.fake_backend import endpoint
from tests.native_capture.test_capture_process_stress import start_port
from tests.multi_run_integration.test_production_audio_saver import _captured_request, _insert_active, _real_saver
from tests.run_persistence.test_audio_repository import ready_runtime
from tests.run_persistence.test_production_run_audio_adapter import _reference
from tests.analysis_scheduler.test_production_executor import (
    _job, _toolchain, _Token, _ResidentAiRuntime, _ResultJudgment, _complete_channel_result,
)


def test_complete_eight_second_stereo_capture_is_the_actual_ai_file_input(tmp_path):
    import time
    rate, frames = 48000, 384000
    adapter = bridge.CaptureProcessManagedInputAdapter(
        config=WorkerConfig(frames_per_buffer=512, max_capture_frames=frames),
        backend_factory="tests.capture_process.fake_backend:create_backend",
        backend_options={"in_range_waveform": True},
    )
    port = None
    runtime = ready_runtime(prepare_run_persistence_database(tmp_path / "ai-input.db"))
    try:
        ready = adapter.open_startup_streams((replace(endpoint(), sample_rate_hz=float(rate)),)).ready_streams[0]
        port = ManagedResidentCapturePort(adapter, (ManagedResidentCaptureRoute(
            "1", ready.endpoint.hardware_id, ready.generation),))
        capture = start_port(port, "1", time.monotonic_ns(), frames)
        assert capture.ready.wait(12)
        complete = capture.entry.completion.result_if_ready().audio
        indices = np.arange(complete.start_frame_cursor, complete.start_frame_cursor + frames, dtype=np.int64)
        # Independent integer-index reference: no sine drift or clipping can hide a gap.
        phase = (indices // 96000) % 4
        expected = np.column_stack(((indices % 2048 - 1024) / 4096 + phase / 16,
                                    (indices % 1024 - 512) / 2048 - phase / 16)).astype(np.float32)
        assert np.max(np.abs(expected)) < 1
        np.testing.assert_array_equal(complete.samples, expected)
        audio = capture.received[0].audio
        assert audio.frame_count == frames and audio.channel_indices == (0, 1)
        run_id, now = capture.reservation.run_id, "2026-09-10T01:00:00.000000Z"
        _insert_active(runtime, run_id=run_id, now=now)
        saver, _ = _real_saver(runtime, now)
        path = tmp_path / "complete-eight-seconds.wav"
        request = _captured_request(run_id, path)
        request = replace(request, audio=audio, source_identity=audio.source_identity,
                          metadata=replace(request.metadata, expected_sample_rate_hz=rate,
                                           expected_frame_count=frames, expected_channel_count=2))
        saved = saver.save_captured(request, SaveCancelToken())
        assert saved.status is AudioSaveStatus.SAVED
        fact = saved.persisted_audio
        reference = _reference(path, audio_id=fact.audio_id, rate=rate, channels=2, frames=frames)
        loaded = saver._authority.open(reference, SimpleNamespace(is_cancelled=False))
        # Preserve the established positive/negative PCM16 quantization rule.
        values = expected.astype(np.float64)
        quantized = np.where(values >= 0, np.floor(values * 32767 + .5),
                             np.ceil(values * 32768 - .5)).astype("<i2")
        inspected = []
        def analyze(kwargs):
            payload = kwargs["request_payload"]
            assert payload["test_run_id"] == run_id
            segment, = payload["segments"]
            assert segment["audio_path"] == str(path.resolve())
            assert segment["sample_rate"] == rate
            assert segment["channels"] == segment["source_channels"] == [0, 1]
            assert segment["channel_map"] == [
                {"logical_channel": 0, "source_channel": 0},
                {"logical_channel": 1, "source_channel": 1}]
            assert segment["duration_ms"] == 8000
            assert payload["window_info"] == {"final_window_offset_ms": 0, "final_window_duration_ms": 8000}
            with wave.open(segment["audio_path"], "rb") as reader:
                assert (reader.getnframes(), reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (
                    frames, rate, 2, 2)
                actual = np.frombuffer(reader.readframes(frames + 1), dtype="<i2").reshape(frames, 2)
            np.testing.assert_array_equal(actual, quantized)
            for phase_index in range(4):
                section = slice(phase_index * 96000, (phase_index + 1) * 96000)
                np.testing.assert_array_equal(actual[section], quantized[section])
                assert np.unique(actual[section, 0]).size > 1000
            inspected.append(payload)
            return {"module_status": "success", "response_payload": {
                "protocol_version": 1, "request_id": kwargs["request_id"],
                "ai_result": {"ai_label": "PASS", "score": .9,
                              "extra": {"decision_threshold": .5, "channel_index": 0}}}}
        ai_runtime = _ResidentAiRuntime(analyze)
        config = {"config_id": 11, "selected_channels": [0, 1],
                  "input_device_samplerate": 48000.0,
                  "channels": [{"channel_index": i, "spl_weighting": "Z"} for i in (0, 1)]}
        job = _job(toolchain=_toolchain(execution_mode="resident_process"), config=config,
                   mapping={"schema": "audio-channel-map/v1", "channel_order": [0, 1]},
                   channel_count=2, audio_dtype="int16", audio_path=str(path.resolve()))
        job = replace(job, run_id=run_id, audio=replace(job.audio, frame_count=frames),
                      snapshot=replace(job.snapshot, analysis_window_end_seconds=8.0),
                      execution_timeout_seconds=15.0)
        class EightSecondJudgment(_ResultJudgment):
            def rejudge(self, *args, **kwargs):
                result = super().rejudge(*args, **kwargs)
                result.analysis_window = (0.0, 8.0)
                return result
        try:
            np.testing.assert_array_equal(loaded.samples, quantized)
            result = ProductionAnalysisExecutor(
                judgment_engine=EightSecondJudgment([_complete_channel_result(i) for i in (0, 1)]),
                ai_runtime=ai_runtime).execute(job, loaded, _Token())
            assert result.status is AnalysisExecutionStatus.SUCCEEDED
            assert len(inspected) == len(ai_runtime.calls) == 1
            (tmp_path / "ai-input-evidence.json").write_text(json.dumps(inspected[0], indent=2), encoding="utf-8")
        finally:
            loaded.close()
    finally:
        if port:
            port.close_and_join(3.0)
        adapter.close_all(3.0)
        runtime.close_after_upstream_stopped()
