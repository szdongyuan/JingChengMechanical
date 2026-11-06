import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.service.multi_run_integration import production_composition as composition
from app.service.capture_process.protocol import PreviewBlock


def frozen(run_id="preview", *, trigger=1_000_000_000, frames=160_000, rate=16_000):
    return SimpleNamespace(
        run_id=run_id, hardware_id="1", trigger_monotonic_ns=trigger,
        target_frame_count=frames, metadata=SimpleNamespace(expected_sample_rate_hz=rate),
        config_payload=composition._payload("config", {"config": {
            "config_id": 1, "selected_channels": [0],
            "channels": [{"channel_index": 0, "stft_nfft": 256, "fft_nfft": 256}],
        }}), calibration_payload=composition._payload("calibration", {}))


@pytest.mark.parametrize("worker", ["production-monitor-display-fanout", "production-live-display-setup"])
@pytest.mark.parametrize("failure_stage", ["construct", "before_start", "after_start"])
def test_preview_constructor_failure_rolls_back_all_started_workers(monkeypatch, worker, failure_stage):
    _check_constructor_rollback(monkeypatch, worker, failure_stage)


def test_preview_constructor_preserves_original_error_when_cleanup_join_raises(monkeypatch):
    _check_constructor_rollback(monkeypatch, "production-live-display-setup", "after_start", fail_join=True)


def _check_constructor_rollback(monkeypatch, worker, failure_stage, *, fail_join=False):
    original_thread = threading.Thread
    original_start, original_join = original_thread.start, original_thread.join
    owned = []
    startup_error = RuntimeError("preview worker startup failed")
    def make_thread(*args, **kwargs):
        if kwargs.get("name") == worker and failure_stage == "construct":
            raise startup_error
        thread = original_thread(*args, **kwargs)
        if kwargs.get("name", "").startswith("production-"):
            owned.append((thread, kwargs["target"].__self__))
        return thread
    def start(thread):
        if thread.name == worker and failure_stage == "before_start":
            raise startup_error
        original_start(thread)
        if thread.name == worker and failure_stage == "after_start":
            raise startup_error
    def join(thread, timeout=None):
        original_join(thread, timeout)
        if fail_join and any(thread is item for item, _owner in owned):
            raise RuntimeError("secondary cleanup failure")
    monkeypatch.setattr(original_thread, "start", start)
    monkeypatch.setattr(original_thread, "join", join)
    monkeypatch.setattr(composition.threading, "Thread", make_thread)
    before = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as raised:
            composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
        assert raised.value is startup_error
        assert time.monotonic() - before < 1.2
        assert not any(thread.is_alive() for thread, _owner in owned)
    finally:
        # Keep the RED regression itself from leaking the reproduced workers.
        for owner in {owner for _thread, owner in owned}:
            with owner._lifecycle_condition:
                owner._closed = True
                owner._lifecycle_condition.notify_all()
            with owner._block_condition:
                owner._block_stopping = True
                owner._block_condition.notify_all()
        for thread, _owner in owned:
            if thread.ident is not None:
                original_join(thread, 1.0)


@pytest.mark.parametrize("initial_preview", [False, True])
def test_preview_past_capture_end_retires_run_after_missing_final_block(initial_preview):
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    value = frozen(frames=640)
    try:
        manager.register_frozen(value)
        assert manager.wait_until_setup_idle(1)
        positions = [0, 960] if initial_preview else [960]
        for sample in positions:
            manager.accept_preview(PreviewBlock(
                "a" * 32, "managed-input-1", "g1", sample // 320, sample,
                value.trigger_monotonic_ns + sample * 1_000_000_000 // 16000,
                16000, np.ones((320, 2), dtype=np.float32)))
            assert manager.wait_until_idle(1)
        assert not manager.has_active_runs()
    finally:
        assert manager.close(2.0)


def test_monitor_setup_failure_cleans_up_partially_started_monitor(monkeypatch):
    stopped, waited = threading.Event(), threading.Event()
    class Monitor:
        def __init__(self, **kwargs):
            self.error_occurred = SimpleNamespace(connect=lambda _slot: None)
        def start(self):
            raise RuntimeError("partially started monitor")
        def stop(self):
            stopped.set()
        def wait(self, _timeout):
            waited.set()
            return True
    monkeypatch.setattr(composition, "AudioMonitor", Monitor)
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    value = frozen()
    value.config_payload = composition._payload("config", {"config": {
        "config_id": 1, "selected_channels": [0], "output_device_name": "test-output"}})
    try:
        manager.register_frozen(value)
        assert manager.wait_until_setup_idle(1)
        assert stopped.is_set() and waited.is_set()
    finally:
        manager.close()


def test_blocked_pipeline_setup_does_not_block_trigger_and_finish_prevents_late_run(monkeypatch):
    entered, release, registered = threading.Event(), threading.Event(), threading.Event()
    original = composition.AnalysisPipeline
    def slow_pipeline(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)
    monkeypatch.setattr(composition, "AnalysisPipeline", slow_pipeline)
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    caller = threading.Thread(target=lambda: (manager.register_frozen(frozen()), registered.set()))
    try:
        caller.start()
        assert entered.wait(1)
        assert registered.wait(.2), "optional pipeline setup blocked trigger registration"
        manager.finish("preview")
        release.set()
        assert manager.wait_until_setup_idle(1)
        assert not manager.has_active_runs()
    finally:
        release.set()
        caller.join(2)
        manager.close()


@pytest.mark.parametrize("blocked_phase", ["setup", "processing"])
def test_real_child_capture_completes_while_optional_preview_is_blocked(monkeypatch, tmp_path, blocked_phase):
    from uuid import uuid4
    from tests.capture_process.test_worker import make_adapter
    from tests.capture_process.fake_backend import endpoint
    from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
        ManagedResidentCapturePort, ManagedResidentCaptureRoute)
    from app.service.multi_run_integration.capture_preview import PreviewCapturePort
    entered, release, started, completed = (threading.Event() for _ in range(4))
    original = composition.AnalysisPipeline
    class Pipeline(original):
        def __init__(self, **kwargs):
            if blocked_phase == "setup":
                entered.set()
                assert release.wait(5)
            super().__init__(**kwargs)
        def process_chunk(self, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().process_chunk(*args, **kwargs)
    monkeypatch.setattr(composition, "AnalysisPipeline", Pipeline)
    adapter = make_adapter()
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    port = None
    caller = None
    outcomes = []
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        manager.attach_capture_client(adapter.client)
        port = PreviewCapturePort(ManagedResidentCapturePort(adapter, (ManagedResidentCaptureRoute(
            "1", ready.endpoint.hardware_id, ready.generation),)), manager)
        run_id = str(uuid4())
        value = frozen(run_id, trigger=time.monotonic_ns(), frames=800, rate=1000)
        def trigger():
            manager.register_frozen(value)
            reservation = port.reserve(run_id, "1").reservation
            port.register_completion(reservation, lambda outcome: (outcomes.append(outcome), completed.set()))
            port.start(reservation, value.trigger_monotonic_ns, value.target_frame_count)
            started.set()
        caller = threading.Thread(target=trigger)
        caller.start()
        assert entered.wait(2)
        assert started.wait(.2), "capture start waited for optional preview"
        run = (manager._runs_by_hardware["managed-input-1"][run_id]
               if blocked_phase == "processing" else None)
        assert completed.wait(3), "child capture waited for parent DSP"
        audio = outcomes[0].audio
        pcm = np.frombuffer(audio.interleaved_bytes, dtype="<f4").reshape(800, 2)
        np.testing.assert_array_equal(np.diff(pcm[:, 0]), np.ones(799))
        np.testing.assert_array_equal(pcm[:, 1] - pcm[:, 0], 1000)
        _verify_saved_analysis_input(tmp_path, run_id, audio, pcm)
        if blocked_phase == "processing":
            assert run.dispatcher.pending_count <= 4
        release.set()
        assert manager.wait_until_setup_idle(2)
        assert not manager.has_active_runs()
    finally:
        release.set()
        if caller is not None:
            caller.join(2)
        if port is not None:
            port.close_and_join(2.0)
        adapter.close_all(2.0)
        manager.close()
    assert not manager._preview_pump.thread.is_alive()
    assert not adapter.client.process.is_alive()


def _verify_saved_analysis_input(tmp_path, run_id, audio, pcm):
    """Use the real save authority and the scheduler/AI's persisted-audio loader."""
    from dataclasses import replace
    from app.service.multi_run_coordinator_mvp.contracts import AudioSaveStatus, SaveCancelToken
    from app.service.run_persistence.prepare_database import prepare_run_persistence_database
    from tests.multi_run_integration.test_production_audio_saver import _captured_request, _insert_active, _real_saver
    from tests.run_persistence.test_audio_repository import ready_runtime
    from tests.run_persistence.test_production_run_audio_adapter import _reference
    runtime = ready_runtime(prepare_run_persistence_database(tmp_path / "preview-save.db"))
    try:
        now = "2026-09-10T01:00:00.000000Z"
        _insert_active(runtime, run_id=run_id, now=now)
        saver, _ = _real_saver(runtime, now)
        path = tmp_path / "complete.wav"
        request = _captured_request(run_id, path)
        request = replace(request, audio=audio, source_identity=audio.source_identity,
                          metadata=replace(request.metadata, expected_sample_rate_hz=audio.sample_rate_hz,
                                           expected_frame_count=audio.frame_count,
                                           expected_channel_count=audio.channel_count))
        outcome = saver.save_captured(request, SaveCancelToken())
        assert outcome.status is AudioSaveStatus.SAVED
        fact = outcome.persisted_audio
        reference = _reference(path, audio_id=fact.audio_id, rate=fact.sample_rate_hz,
                               channels=fact.channel_count, frames=fact.frame_count)
        loaded = saver._authority.open(reference, SimpleNamespace(is_cancelled=False))
        try:
            assert loaded.frame_count == 800
            clipped = np.clip(pcm.astype(np.float64), -1.0, 1.0)
            expected = np.where(clipped >= 0, np.floor(clipped * 32767 + .5),
                                np.ceil(clipped * 32768 - .5)).astype("<i2")
            np.testing.assert_array_equal(loaded.samples, expected)
        finally:
            loaded.close()
    finally:
        runtime.close_after_upstream_stopped()


@pytest.mark.parametrize("gap", ["sample", "sequence", "generation"])
def test_preview_keeps_true_sample_positions_and_resets_real_stft_on_gaps(monkeypatch, gap):
    pipelines = []
    frames = []
    class Pipeline(composition.AnalysisPipeline):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.resets = 0
            self.times = []
            pipelines.append(self)
        def reset(self):
            self.resets += 1
            super().reset()
        def process_chunk(self, mono, elapsed_time, **kwargs):
            self.times.append(elapsed_time)
            return super().process_chunk(mono, elapsed_time, **kwargs)
    monkeypatch.setattr(composition, "AnalysisPipeline", Pipeline)
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace(
        stft_updated=SimpleNamespace(emit=lambda *args: frames.append(args))))
    value = frozen()
    try:
        manager.register_frozen(value)
        assert manager.wait_until_setup_idle(1)
        run = manager._runs_by_hardware["managed-input-1"][value.run_id]
        # First delivered preview begins 20 ms into the recording.
        def offer(sequence, first_sample, relative_ns, generation="g1"):
            manager.accept_preview(PreviewBlock("a" * 32, "managed-input-1", generation,
                sequence, first_sample, value.trigger_monotonic_ns + relative_ns,
                16_000, np.ones((320, 2), dtype=np.float32)))
            assert manager.wait_until_idle(1)
            assert run.dispatcher.wait_until_idle(1)
        offer(1, 1000, 20_000_000)
        assert pipelines[0].times == [.04]
        assert frames, "the actual realtime STFT must still emit"
        # Same-generation positions follow sample cursors, despite clock jitter.
        offer(2, 1320, 43_000_000)
        assert pipelines[0].times[-1] == .06
        assert pipelines[0].resets == 0
        if gap == "sample":
            offer(3, 2280, 100_000_000)
            assert pipelines[0].times[-1] == .12
        elif gap == "sequence":
            offer(4, 1640, 60_000_000)
            assert pipelines[0].times[-1] == .08
        else:
            offer(3, 0, 60_000_000, "g2")
            assert pipelines[0].times[-1] == .08
        assert pipelines[0].resets == 1
    finally:
        manager.close()


def test_blocked_setup_has_bounded_queue_and_shutdown(monkeypatch):
    entered, release, registered = threading.Event(), threading.Event(), threading.Event()
    original = composition.AnalysisPipeline
    def slow_pipeline(**kwargs):
        entered.set()
        release.wait(3)
        return original(**kwargs)
    monkeypatch.setattr(composition, "AnalysisPipeline", slow_pipeline)
    manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace())
    caller = threading.Thread(target=lambda: (manager.register_frozen(frozen()), registered.set()))
    try:
        caller.start()
        assert entered.wait(1)
        assert registered.wait(.2), "optional setup blocked trigger registration"
        for index in range(100):
            manager.register_frozen(frozen(str(index)))
        assert manager._registrations_inflight <= 6
        before = time.monotonic()
        assert manager.close(.05) is False
        assert time.monotonic() - before < .3
        release.set()
        assert manager.wait_until_setup_idle(2)
        assert not manager.has_active_runs()
    finally:
        release.set()
        caller.join(2)
        manager.close()
