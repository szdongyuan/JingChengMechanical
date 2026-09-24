"""Full native/Resident child evidence. No PortAudio hardware is opened."""
import ctypes as ct
from dataclasses import replace
import hashlib
import json
import os
import sys
from multiprocessing import shared_memory
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import contracts as c
from app.service.capture_process import adapter as bridge
from app.service.capture_process.protocol import WorkerConfig
from app.service.managed_input_contracts import ManagedInputCloseStatus
from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
    ManagedResidentCapturePort, ManagedResidentCaptureRoute, ManagedResidentCaptureCloseStatus,
)
from tests.capture_process.fake_backend import endpoint

RATE, WINDOW = 48000, 384000


def wait_for(read, predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if predicate(value):
            return value
        assert time.monotonic() < deadline, value
        time.sleep(.02)


def hold_parent_gil(milliseconds):
    sleep = ct.PyDLL("kernel32").Sleep
    sleep.argtypes, sleep.restype = [ct.c_uint32], None
    started = time.perf_counter()
    sleep(milliseconds)
    elapsed = time.perf_counter() - started
    assert elapsed > 10 if milliseconds > 10000 else elapsed >= 2.2
    return elapsed


def producer_reference(frames):
    return np.concatenate([
        (np.arange((512 - index % 3) * 2, dtype=np.float32)
         + index * 10000).reshape(-1, 2)
        for index in range(frames // 511 + 2)
    ])[:frames]


def start_port(port, hardware, trigger, frames):
    result = port.reserve(str(uuid4()), hardware)
    assert result.reservation is not None, result
    reservation = result.reservation
    entry = port._reservations[reservation.reservation_id]
    received, ready = [], threading.Event()
    port.register_completion(reservation, lambda outcome: (received.append(outcome), ready.set()))
    assert entry.completion is entry.target.pool.capture_completion(entry.binding)
    port.start(reservation, trigger, frames)
    return SimpleNamespace(reservation=reservation, entry=entry, received=received, ready=ready)


def exact_capture(capture, expected, start, generation):
    assert capture.ready.wait(5)
    outcome = capture.entry.completion.result_if_ready()
    assert outcome.status is c.CaptureOutcomeStatus.COMPLETED, outcome
    assert outcome.binding is capture.entry.binding
    assert outcome.audio.binding is capture.entry.binding
    assert outcome.audio.start_frame_cursor == start
    assert outcome.audio.frame_count == len(expected)
    target = capture.entry.target
    assert generation == f"{target.resident_hardware_id}:generation-{target.generation.stream_generation}"
    np.testing.assert_array_equal(outcome.audio.samples, expected)
    delivered = capture.received[0]
    assert delivered.reservation is capture.reservation
    assert delivered.audio.sample_rate_hz == RATE
    assert delivered.audio.channel_count == 2
    assert delivered.audio.channel_indices == (0, 1)
    assert delivered.audio.frame_count == len(expected)
    assert delivered.audio.interleaved_bytes == expected.tobytes()
    for phase in range(len(expected) // (RATE * 2)):
        section = slice(phase * RATE * 2, (phase + 1) * RATE * 2)
        np.testing.assert_array_equal(outcome.audio.samples[section], expected[section])
    return hashlib.sha256(delivered.audio.interleaved_bytes).hexdigest()


def open_native(tmp_path):
    cleanup = tmp_path / "native-cleanup.json"
    adapter = bridge.CaptureProcessManagedInputAdapter(
        config=WorkerConfig(frames_per_buffer=512, max_capture_frames=WINDOW,
                            startup_stream_timeout_s=2.0),
        backend_factory="tests.native_capture.process_backend:create_backend",
        backend_options={"cleanup_path": str(cleanup)},
    )
    port = None
    try:
        endpoints = tuple(replace(endpoint(i), sample_rate_hz=float(RATE), hostapi_name="WASAPI")
                          for i in (1, 2))
        ready = adapter.open_startup_streams(endpoints).ready_streams
        assert len(ready) == 2
        port = ManagedResidentCapturePort(adapter, tuple(ManagedResidentCaptureRoute(
            str(item.endpoint.hardware_id.value), item.endpoint.hardware_id, item.generation) for item in ready))
        return adapter, port, cleanup
    except BaseException as failure:
        # Ownership transfers only on return, including partial startup failures.
        if port is not None:
            try:
                port.close_and_join(3.0)
            except BaseException as cleanup_error:
                failure.add_note(f"Capture port cleanup also failed: {cleanup_error}")
        try:
            adapter.close_all(3.0)
        except BaseException as cleanup_error:
            failure.add_note(f"Capture adapter cleanup also failed: {cleanup_error}")
        raise


@pytest.mark.parametrize("failure", ["partial-startup", "port-construction"])
@pytest.mark.parametrize("cleanup_raises", [False, True], ids=["clean-close", "close-reports-error"])
def test_open_native_setup_failure_reaps_owned_resources(monkeypatch, tmp_path, failure, cleanup_raises):
    original_adapter = bridge.CaptureProcessManagedInputAdapter
    original_endpoint = endpoint
    adapters = []
    construction_error = RuntimeError("injected port constructor failure")

    class TrackingAdapter(original_adapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            adapters.append(self)

        def close_all(self, timeout_seconds):
            result = super().close_all(timeout_seconds)
            if cleanup_raises:
                raise RuntimeError("injected cleanup report after owned resources closed")
            return result

    monkeypatch.setattr(bridge, "CaptureProcessManagedInputAdapter", TrackingAdapter)
    if failure == "partial-startup":
        monkeypatch.setattr(sys.modules[__name__], "endpoint", lambda number:
                            replace(original_endpoint(number), input_channels=65)
                            if number == 2 else original_endpoint(number))
        expected_error = AssertionError
    else:
        def failing_port(*args, **kwargs):
            raise construction_error
        monkeypatch.setattr(sys.modules[__name__], "ManagedResidentCapturePort", failing_port)
        expected_error = RuntimeError
    try:
        with pytest.raises(expected_error) as raised:
            open_native(tmp_path)
        if failure == "port-construction":
            assert raised.value is construction_error, "cleanup must preserve the original setup failure"
        adapter, = adapters
        ready_count = len(adapter._startup_result.ready_streams)
        assert ready_count == (1 if failure == "partial-startup" else 2)
        assert not adapter.client.process.is_alive(), "setup failure leaked the owned native child"
        assert not adapter._materializer.is_alive()
        assert not adapter.client._sender.is_alive()
        assert not adapter.client._receiver.is_alive()
        cleanup = json.loads((tmp_path / "native-cleanup.json").read_text(encoding="utf-8"))
        assert cleanup["streams_closed"] and cleanup["native_closed"]
        assert len(cleanup["closed"]) == ready_count
        assert all(item["driver_result"] == 2 for item in cleanup["closed"])
        for handle in adapter.client.preview.bootstrap.streams:
            try:
                leaked = shared_memory.SharedMemory(name=handle.shm_name)
            except FileNotFoundError:
                continue
            leaked.close()
            pytest.fail("setup failure leaked preview shared memory")
        if cleanup_raises:
            assert any("injected cleanup report" in note for note in raised.value.__notes__)
    finally:
        # Keep the RED regression itself leak-free, bypassing the report injection.
        for adapter in adapters:
            original_adapter.close_all(adapter, 3.0)


def assert_healthy(snapshot, original):
    assert snapshot["pid"] == original["pid"] != os.getpid()
    for hardware, state in snapshot["streams"].items():
        assert state["generation"] == original["streams"][hardware]["generation"]
        assert abs(state["origin_ns"] - original["streams"][hardware]["origin_ns"]) <= 1
        assert state["fault"] is None and state["overflow"] == state["error"] == 0
        assert state["driver_priority"] == 2 and state["priority_applied"] == 1
        assert state["driver_result"] == 0
        assert state["sequence"] >= state["consumed"] > 0


def assert_resources_closed(adapter, port, cleanup, names):
    try:
        report = port.close_and_join(3.0)
    finally:
        closed = adapter.close_all(3.0)
    assert report.status is ManagedResidentCaptureCloseStatus.CLOSED
    assert all(item.status is ManagedInputCloseStatus.CLOSED for item in closed)
    assert not adapter.client.process.is_alive()
    assert not adapter._materializer.is_alive()
    assert not adapter.client._sender.is_alive()
    assert not adapter.client._receiver.is_alive()
    report = json.loads(cleanup.read_text(encoding="utf-8"))
    assert report["streams_closed"] and report["native_closed"]
    assert len(report["closed"]) == 2
    assert all(item["driver_result"] in (0, 2) for item in report["closed"])
    for name in names:
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)


@pytest.mark.parametrize("loaded", [False, True], ids=["simple", "parent-load-and-10s-gil"])
def test_two_native_child_streams_exact_adjoining_eight_second_windows(monkeypatch, tmp_path, loaded):
    release, copying, stop_load = threading.Event(), threading.Event(), threading.Event()
    copy = bridge.copy_completed_audio
    def held_copy(*args, **kwargs):
        copying.set()
        assert release.wait(60), "test did not release parent bulk copy"
        return copy(*args, **kwargs)
    if loaded:
        monkeypatch.setattr(bridge, "copy_completed_audio", held_copy)
    else:
        release.set()
    adapter, port, cleanup = open_native(tmp_path)
    inspect = lambda: adapter.client.request("test_native_snapshot")
    locks, names, outputs, manager, load_thread = [], [], [], None, None
    started = time.perf_counter()
    try:
        initial = inspect()
        names.extend(handle.shm_name for handle in adapter.client.preview.bootstrap.streams)
        if loaded:
            from app.service.multi_run_integration import production_composition as composition
            from tests.capture_process.test_preview import frozen
            stft, pipelines = [], []
            class MeasuredPipeline(composition.AnalysisPipeline):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.resets, self.elapsed = 0, []
                    pipelines.append(self)

                def reset(self):
                    self.resets += 1
                    super().reset()

                def process_chunk(self, mono, elapsed_time, **kwargs):
                    self.elapsed.append(elapsed_time)
                    return super().process_chunk(mono, elapsed_time, **kwargs)

            monkeypatch.setattr(composition, "AnalysisPipeline", MeasuredPipeline)
            manager = composition._ProductionLiveDisplayManager(bus=SimpleNamespace(
                stft_updated=SimpleNamespace(emit=lambda *args: stft.append(args))))
            manager.attach_capture_client(adapter.client)
            for hardware, state in initial["streams"].items():
                value = frozen(hardware, trigger=state["origin_ns"], frames=WINDOW * 3, rate=RATE)
                value.hardware_id = hardware.removeprefix("managed-input-")
                manager.register_frozen(value)
            assert manager.wait_until_setup_idle(2)
            def parent_load():
                samples = np.linspace(-.8, .8, 8192)
                while not stop_load.wait(.003):
                    np.fft.rfft(samples)
                    json.dumps({str(i): list(range(128)) for i in range(30)})
            load_thread = threading.Thread(target=parent_load, name="test-parent-analysis-load")
            load_thread.start()
            wait_for(lambda: len(stft), bool, 3)
        first = {hardware: start_port(port, hardware.removeprefix("managed-input-"),
                                     state["origin_ns"], WINDOW)
                 for hardware, state in initial["streams"].items()}
        before = wait_for(inspect, lambda value: all(s["cursor"] >= RATE * 7.5
                                                   for s in value["streams"].values()))
        pause = 0
        stalled, preview_metrics = None, None
        if loaded:
            for handle in adapter.client.preview.bootstrap.streams:
                assert handle.lock.acquire(timeout=2)
                locks.append(handle.lock)
            assert manager.wait_until_idle(2)
            assert len(pipelines) == 2
            prior_preview = [(pipeline.resets, pipeline.elapsed[-1], len(pipeline.elapsed))
                             for pipeline in pipelines]
            pause = hold_parent_gil(10500)
            assert copying.wait(2)
            stalled = inspect()
            assert stalled["pending_count"] == 2
            assert len(stalled["captures"]) == 2
            assert all(item["status"] == "COMPLETED" and item["shm_name"]
                       for item in stalled["captures"])
            names.extend(item["shm_name"] for item in stalled["captures"])
            for hardware, state in stalled["streams"].items():
                assert state["cursor"] > WINDOW
                assert state["oldest"] > 0, "first result must survive history rollover"
                assert state["oldest"] < WINDOW, "adjacent start must still be in history"
                assert state["sequence"] > before["streams"][hardware]["sequence"] + 100
                assert state["dropped_preview"] > before["streams"][hardware]["dropped_preview"]
            assert all(not item.ready.is_set() for item in first.values())
        controls_started = time.perf_counter()
        second = {hardware: start_port(port, hardware.removeprefix("managed-input-"),
                                      state["origin_ns"] + 8_000_000_000, WINDOW)
                  for hardware, state in initial["streams"].items()}
        controls_seconds = time.perf_counter() - controls_started
        assert controls_seconds < 2
        if loaded:
            assert adapter.client.request("stats")["pending_count"] == 4
            # Two remaining credits plus retained first/active second windows exhaust six.
            target = next(iter(first.values())).entry.target
            reserved = [target.pool.reserve_capture(uuid4().hex, target.resident_hardware_id) for _ in range(2)]
            with pytest.raises(c.ResidentRequestError) as capacity:
                target.pool.reserve_capture(uuid4().hex, target.resident_hardware_id)
            assert capacity.value.error.code is c.ResidentErrorCode.CAPACITY
            for binding in reserved:
                target.pool.cancel_capture(binding)
        finished = wait_for(inspect, lambda value: all(s["cursor"] >= WINDOW * 2
                                                     for s in value["streams"].values()))
        assert_healthy(finished, initial)
        if loaded:
            retained = wait_for(inspect, lambda value: len(value["captures"]) == 4 and
                                all(item["shm_name"] for item in value["captures"]))
            names.extend(item["shm_name"] for item in retained["captures"])
            assert retained["pending_count"] == 4
            assert all(not item.ready.is_set() for item in first.values())
            release.set()
            for lock in locks:
                lock.release()
            locks.clear()
            emitted_before = len(stft)
            wait_for(lambda: len(stft), lambda value: value > emitted_before, 3)
            gaps = []
            for pipeline, (resets, elapsed, count) in zip(pipelines, prior_preview, strict=True):
                # The four-slot ring can still deliver a few pre-stall previews.
                # Wait for the first genuinely new span, not merely any delivery.
                wait_for(lambda: pipeline.elapsed[-1], lambda value: value - elapsed > 4, 3)
                assert pipeline.resets > resets, "resumed real STFT must reset across the skipped preview span"
                spans = [elapsed, *pipeline.elapsed[count:]]
                gap = max(end - start for start, end in zip(spans, spans[1:]))
                assert gap > 4, "preview gaps must preserve elapsed sample time"
                gaps.append(gap)
            preview_metrics = {"stft_emissions": len(stft),
                               "gap_seconds": gaps,
                               "resets": [p.resets for p in pipelines]}
        reference = producer_reference(WINDOW * 2)
        for hardware, state in initial["streams"].items():
            outputs.append(exact_capture(first[hardware], reference[:WINDOW], 0, state["generation"]))
            outputs.append(exact_capture(second[hardware], reference[WINDOW:], WINDOW, state["generation"]))
        wait_for(lambda: adapter.client.request("stats")["pending_count"], lambda value: value == 0)
        if loaded:
            expired = start_port(port, "1", initial["streams"]["managed-input-1"]["origin_ns"], WINDOW)
            assert expired.ready.wait(3)
            outcome = expired.entry.completion.result_if_ready()
            assert outcome.status is c.CaptureOutcomeStatus.FAILED
            assert outcome.error.code is c.ResidentErrorCode.EXPIRED
        evidence = {"scenario": "loaded" if loaded else "simple", "parent_gil_seconds": pause,
                    "seconds": time.perf_counter() - started, "second_controls_seconds": controls_seconds,
                    "initial": initial, "final": finished, "sha256": outputs,
                    "before_pause": before, "after_pause": stalled, "preview": preview_metrics,
                    "frames_per_window": WINDOW, "sections_per_window": 4}
        (tmp_path / "native-stress-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print("NATIVE_CHILD_EVIDENCE " + json.dumps(evidence))
    finally:
        release.set()
        stop_load.set()
        if load_thread:
            load_thread.join(2)
            assert not load_thread.is_alive()
        for lock in locks:
            lock.release()
        try:
            if manager:
                assert manager.close(3)
        finally:
            assert_resources_closed(adapter, port, cleanup, names)


def test_native_child_recovers_timestamped_windows_after_ordinary_parent_stall(tmp_path):
    adapter, port, cleanup = open_native(tmp_path)
    inspect = lambda: adapter.client.request("test_native_snapshot")
    names = [handle.shm_name for handle in adapter.client.preview.bootstrap.streams]
    try:
        initial = inspect()
        hold_parent_gil(2300)
        after = inspect()
        assert_healthy(after, initial)
        captures = []
        for hardware, state in initial["streams"].items():
            assert after["streams"][hardware]["cursor"] > RATE
            for window in range(2):
                capture = start_port(port, hardware.removeprefix("managed-input-"),
                                     state["origin_ns"] + window * 500_000_000, RATE // 2)
                captures.append((capture, state["generation"], window))
        reference = producer_reference(RATE)
        for capture, generation, window in captures:
            exact_capture(capture, reference[window * RATE // 2:(window + 1) * RATE // 2],
                          window * RATE // 2, generation)
    finally:
        assert_resources_closed(adapter, port, cleanup, names)
