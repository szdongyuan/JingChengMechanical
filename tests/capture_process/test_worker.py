import os
import time

import numpy as np
import pytest
import threading

from app.service.capture_process.protocol import WorkerConfig
from app.service.analysis.resident_audio_mvp import CaptureOutcomeStatus
from tests.capture_process.fake_backend import endpoint


def make_adapter(**kwargs):
    from app.service.capture_process.adapter import CaptureProcessManagedInputAdapter
    return CaptureProcessManagedInputAdapter(
        config=kwargs.pop("config", WorkerConfig(frames_per_buffer=20, max_capture_frames=2000,
                            startup_stream_timeout_s=1.0)),
        backend_factory="tests.capture_process.fake_backend:create_backend",
        **kwargs,
    )


def test_spawned_child_is_isolated_and_first_capture_preserves_pcm():
    adapter = make_adapter()
    try:
        startup = adapter.open_startup_streams((endpoint(),))
        assert len(startup.ready_streams) == 1
        info = adapter.client.diagnostics
        assert info["pid"] != os.getpid()
        assert info["numeric_environment"] == dict.fromkeys(
            ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"), "1")
        assert info["numeric_runtime_threads"] and set(info["numeric_runtime_threads"].values()) == {1}
        assert info["forbidden_modules"] == ()
        if os.name == "nt":
            assert info["priority_class"] == 0x8000
        ready = startup.ready_streams[0]
        target = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        binding = target.pool.reserve_capture("first", target.resident_hardware_id)
        completion = target.pool.capture_completion(binding)
        assert target.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(),
                                         target_frame_count=80) is completion
        outcome = completion.wait(3)
        assert outcome.status is CaptureOutcomeStatus.COMPLETED
        assert outcome.binding is binding and outcome.audio.binding is binding
        assert outcome.audio.samples.shape == (80, 2)
        np.testing.assert_array_equal(np.diff(outcome.audio.samples[:, 0]), np.ones(79))
        np.testing.assert_array_equal(outcome.audio.samples[:, 1] - outcome.audio.samples[:, 0], 1000)
        assert not outcome.audio.samples.flags.writeable
    finally:
        adapter.close_all(2.0)
    assert not adapter.client.process.is_alive()


def test_conservative_reservation_can_shrink_without_returning_count_credit():
    from uuid import uuid4
    import pytest
    from app.service.capture_process.protocol import CaptureLimits, TransportCapacityError
    from app.service.capture_process.transport import CompletedAudioStore
    store = CompletedAudioStore(uuid4().hex, CaptureLimits(100, (2,), max_captures=1))
    try:
        reserved = store.reserve("held", frames=100, channels=2, sample_rate_hz=1000)
        smaller = store.shrink(reserved, frames=20)
        assert store.pending_count == 1 and store.reserved_bytes == 160
        with pytest.raises(TransportCapacityError):
            store.reserve("second", frames=1, channels=2, sample_rate_hz=1000)
        descriptor = store.publish(smaller, np.zeros((20, 2), dtype=np.float32))
        assert descriptor.frames == 20
        assert store.receipt(descriptor)
    finally:
        store.close()


def test_initial_endpoint_coordinates_are_resolved_inside_child():
    adapter = make_adapter(backend_options={"resolver": True, "require_resolved": True})
    try:
        assert len(adapter.open_startup_streams((endpoint(),)).ready_streams) == 1
    finally:
        adapter.close_all(2.0)


@pytest.mark.parametrize("partial", [False, True])
def test_spawned_startup_failure_retains_backend_detail(partial):
    from app.service.capture_process.values import pack, unpack
    from app.service.managed_input_contracts import ManagedInputCloseStatus
    adapter = make_adapter(backend_options={"fail_devices": (1,)})
    try:
        startup = adapter.open_startup_streams(
            (endpoint(1), endpoint(2)) if partial else (endpoint(1),)
        )
        assert [item.endpoint.hardware_id.value for item in startup.ready_streams] == ([2] if partial else [])
        assert len(startup.failures) == 1
        failure = startup.failures[0]
        assert failure.error_code == "resident_open_failed"
        assert "synthetic open failure" in getattr(failure, "detail", "")
        assert unpack(pack(startup)) == startup
        assert adapter.client.diagnostics["pid"] != os.getpid()
        assert adapter.client.diagnostics["forbidden_modules"] == ()
    finally:
        closed = adapter.close_all(2.0)
    assert closed[0].status is ManagedInputCloseStatus.NOT_OPEN
    assert adapter.client.process.exitcode == 0


def test_readiness_does_not_wait_on_child_bulk_publication():
    adapter = make_adapter(backend_options={"delay_publish_s": .8})
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        binding = route.pool.reserve_capture("bulk", route.resident_hardware_id)
        route.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=20)
        time.sleep(.15)
        ticket = route.pool.begin_readiness(route.resident_hardware_id, time.monotonic_ns() + 2_000_000_000)
        assert ticket.future.wait(.3) is not None
        assert route.pool.capture_completion(binding).wait(3).status is CaptureOutcomeStatus.COMPLETED
    finally:
        adapter.close_all(2.0)


def test_partial_open_and_recovery_use_existing_resident_semantics():
    from app.service.analysis.resident_audio_mvp.contracts import LifecycleStatus, ResidentRequestError
    from app.service.managed_input_contracts import ManagedInputCloseStatus
    adapter = make_adapter(backend_options={"resolver": True, "fail_devices": (12,), "fault_after_frames": 300})
    try:
        startup = adapter.open_startup_streams((endpoint(), endpoint(2)))
        assert [item.endpoint.hardware_id.value for item in startup.ready_streams] == [1]
        assert [item.hardware_id.value for item in startup.failures] == [2]
        ready = startup.ready_streams[0]
        route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        initial = route.pool.begin_readiness(route.resident_hardware_id, time.monotonic_ns() + 2_000_000_000)
        generation = initial.future.wait(1).generation_id
        deadline = time.monotonic() + 4
        recovered = None
        while time.monotonic() < deadline:
            ticket = route.pool.begin_readiness(route.resident_hardware_id, time.monotonic_ns() + 2_000_000_000)
            recovered = ticket.future.wait(2)
            if recovered.generation_id is not None and recovered.generation_id != generation:
                break
            time.sleep(.04)
        assert recovered.status in (LifecycleStatus.GENERATION_CONTINUOUS, LifecycleStatus.GENERATION_REPLACED)
        assert recovered.generation_id != generation
        with pytest.raises(ResidentRequestError):
            route.pool.reserve_capture("stale", route.resident_hardware_id, generation_id=generation)
        binding = route.pool.reserve_capture("recovered", route.resident_hardware_id,
                                              generation_id=recovered.generation_id)
        assert route.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(),
                                         target_frame_count=80).wait(2).status is CaptureOutcomeStatus.COMPLETED
    finally:
        results = adapter.close_all(2.0)
    assert results[0].status is ManagedInputCloseStatus.CLOSED
    assert results[1].status is ManagedInputCloseStatus.NOT_OPEN


def test_factory_failure_and_parent_disconnect_reap_only_owned_child():
    import multiprocessing
    from app.service.capture_process.client import CaptureProcessError
    before = {item.pid for item in multiprocessing.active_children()}
    failed = make_adapter(backend_options={"factory_failure": True})
    with pytest.raises(CaptureProcessError, match="synthetic startup failure"):
        failed.open_startup_streams((endpoint(),))
    assert {item.pid for item in multiprocessing.active_children()} == before
    adapter = make_adapter()
    adapter.open_startup_streams((endpoint(),))
    child = adapter.client.process
    adapter.client.connection.close()
    child.join(3)
    assert not child.is_alive()
    adapter.close_all(.5)


@pytest.mark.parametrize("pipe_close_error", [False, True])
def test_ambiguous_start_timeout_fences_late_reply_without_retry(monkeypatch, pipe_close_error):
    from app.service.capture_process.client import CaptureProcessError
    config = WorkerConfig(frames_per_buffer=20, max_capture_frames=2000,
                          startup_stream_timeout_s=1.0, control_timeout_s=.15)
    adapter = make_adapter(config=config, backend_options={"delay_start_reply_s": .6})
    original_close = None
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        binding = route.pool.reserve_capture("timeout", route.resident_hardware_id)
        original_close = adapter.client.connection.close
        if pipe_close_error:
            def broken_close():
                raise OSError(6, "invalid Windows handle")
            monkeypatch.setattr(adapter.client.connection, "close", broken_close)
        with pytest.raises(TimeoutError, match="not retried"):
            route.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=1000)
        outcome = binding._completion.wait(1)
        assert outcome.status is CaptureOutcomeStatus.FAILED
        with pytest.raises(CaptureProcessError):
            route.pool.reserve_capture("later", route.resident_hardware_id)
        time.sleep(.65)
        assert binding._completion.result_if_ready() is outcome
    finally:
        if original_close is not None:
            monkeypatch.setattr(adapter.client.connection, "close", original_close)
        adapter.close_all(1.0)
    assert not adapter.client.process.is_alive()


def test_shrink_rejects_growth_stale_reservation_and_published_capture():
    from uuid import uuid4
    from app.service.capture_process.protocol import CaptureLimits
    from app.service.capture_process.transport import CompletedAudioStore
    with CompletedAudioStore(uuid4().hex, CaptureLimits(100, (2,))) as store:
        reservation = store.reserve("bounded", frames=100, channels=2, sample_rate_hz=1000)
        with pytest.raises(ValueError):
            store.shrink(reservation, frames=101)
        smaller = store.shrink(reservation, frames=20)
        with pytest.raises(ValueError):
            store.shrink(reservation, frames=10)
        descriptor = store.publish(smaller, np.zeros((20, 2), dtype=np.float32))
        with pytest.raises(ValueError):
            store.shrink(smaller, frames=10)
        store.receipt(descriptor)


def test_preview_first_frame_timestamp_and_cursor_match_complete_audio():
    adapter = make_adapter(backend_options={"start_return_delay": .06})
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        hardware_id = f"managed-input-{ready.endpoint.hardware_id.value}"
        time.sleep(.12)
        blocks = adapter.client.preview.read(hardware_id)
        assert blocks
        for block in blocks:
            assert block.first_sample == int(block.pcm[0, 0])
            assert block.generation_id == hardware_id + ":generation-1"
        first = blocks[0]
        route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        binding = route.pool.reserve_capture("preview-time", hardware_id)
        outcome = route.pool.start_capture(binding, trigger_monotonic_ns=first.monotonic_ns,
                                            target_frame_count=len(first.pcm)).wait(3)
        assert outcome.status is CaptureOutcomeStatus.COMPLETED
        np.testing.assert_array_equal(outcome.audio.samples, first.pcm)
    finally:
        adapter.close_all(2.0)


def test_shrink_is_rejected_while_publication_holds_a_lease(monkeypatch):
    from uuid import uuid4
    from app.service.capture_process.protocol import CaptureLimits
    from app.service.capture_process import transport
    entered, release = threading.Event(), threading.Event()
    original = transport._write_pcm
    def hold(mapping, pcm):
        entered.set()
        assert release.wait(2)
        original(mapping, pcm)
    monkeypatch.setattr(transport, "_write_pcm", hold)
    store = transport.CompletedAudioStore(uuid4().hex, CaptureLimits(100, (2,)))
    reservation = store.reserve("publishing", frames=100, channels=2, sample_rate_hz=1000)
    results = []
    thread = threading.Thread(target=lambda: results.append(store.publish(reservation, np.zeros((100, 2), dtype=np.float32))))
    try:
        thread.start()
        assert entered.wait(1)
        with pytest.raises(ValueError):
            store.shrink(reservation, frames=20)
        assert store.reserved_bytes == 800 and store.pending_count == 1
    finally:
        release.set()
        thread.join(2)
        store.close()


def test_parent_control_failure_disconnects_child_without_waiting_for_owner_close(monkeypatch):
    from app.service.capture_process.client import CaptureProcessError
    adapter = make_adapter()
    def broken_event(message):
        raise ValueError("malformed control event")
    monkeypatch.setattr(adapter, "_on_event", broken_event)
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
        binding = route.pool.reserve_capture("broken-reader", route.resident_hardware_id)
        completion = route.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=20)
        assert completion.wait(2).status is CaptureOutcomeStatus.FAILED
        adapter.client.process.join(2)
        assert not adapter.client.process.is_alive()
    finally:
        adapter.close_all(.5)


def test_bootstrap_rejects_live_options_before_allocating_resources():
    adapter = make_adapter(backend_options={"event": threading.Event()})
    with pytest.raises(TypeError, match="allowed capture-process value"):
        adapter.open_startup_streams((endpoint(),))
    assert adapter.client is None


def test_worker_value_codec_rejects_resident_tokens_and_unbounded_messages():
    from app.service.capture_process.values import pack
    from app.service.analysis.resident_audio_mvp.contracts import CaptureCompletion
    with pytest.raises(TypeError):
        pack(CaptureCompletion())
    with pytest.raises(ValueError):
        pack({"oversized": "x" * 65536})


def test_unjoined_child_bulk_publisher_reports_nonclean_shutdown():
    from app.service.managed_input_contracts import ManagedInputCloseStatus
    config = WorkerConfig(frames_per_buffer=20, max_capture_frames=2000,
                          shutdown_timeout_s=.3, startup_stream_timeout_s=1.0)
    adapter = make_adapter(config=config, backend_options={"delay_publish_s": 2.0})
    ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
    route = adapter.capture_target(ready.endpoint.hardware_id, ready.generation)
    binding = route.pool.reserve_capture("shutdown-publisher", route.resident_hardware_id)
    route.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=20)
    time.sleep(.15)
    before = time.monotonic()
    assert adapter.close_all(2.0)[0].status is ManagedInputCloseStatus.FAILED
    assert time.monotonic() - before < 2.4
    assert not adapter.client.process.is_alive()


@pytest.mark.parametrize("failure_point", [
    "pipe", "process", "sender_constructor", "receiver_constructor",
    "process_start", "process_start_after", "sender_start", "receiver_start_after",
    "process_pipe_cleanup_error", "process_preview_cleanup_error",
])
def test_partial_client_acquisition_releases_every_acquired_resource(monkeypatch, failure_point):
    from multiprocessing import shared_memory
    from app.service.capture_process import client as client_module
    preview_rings, pipes, processes, threads = [], [], [], []
    context = client_module.multiprocessing.get_context("spawn")
    original_preview = client_module.PreviewRing.create
    original_thread = threading.Thread
    failure = RuntimeError(f"injected {failure_point} failure")
    config = WorkerConfig(frames_per_buffer=20, max_capture_frames=2000,
                          control_timeout_s=.5, shutdown_timeout_s=.5)
    adapter = make_adapter(config=config)
    def create_preview(*args, **kwargs):
        ring = original_preview(*args, **kwargs)
        preview_rings.append(ring)
        if failure_point == "process_preview_cleanup_error":
            original_close = ring.close
            def failed_cleanup(timeout_s):
                original_close(timeout_s)
                raise OSError("secondary preview cleanup failure")
            ring.close = failed_cleanup
        return ring
    class Context:
        Lock = staticmethod(context.Lock)
        def Pipe(self, **kwargs):
            if failure_point == "pipe":
                raise failure
            result = context.Pipe(**kwargs)
            pipes.extend(result)
            if failure_point == "process_pipe_cleanup_error":
                original_close = result[0].close
                def failed_cleanup():
                    original_close()
                    raise OSError("secondary pipe cleanup failure")
                result[0].close = failed_cleanup
            return result
        def Process(self, **kwargs):
            if failure_point in ("process", "process_pipe_cleanup_error", "process_preview_cleanup_error"):
                raise failure
            process = context.Process(**kwargs)
            processes.append(process)
            original_start = process.start
            if failure_point in ("process_start", "process_start_after"):
                def failed_start():
                    if failure_point.endswith("_after"):
                        del process.start
                        original_start()
                    raise failure
                process.start = failed_start
            return process
    def create_thread(*args, **kwargs):
        name = kwargs.get("name", "")
        parent = name.startswith("capture-parent-control-")
        role = "sender" if name.endswith("writer") else "receiver"
        if parent and failure_point == role + "_constructor":
            raise failure
        thread = original_thread(*args, **kwargs)
        if parent:
            threads.append(thread)
            original_start = thread.start
            if failure_point in (role + "_start", role + "_start_after"):
                def failed_start():
                    if failure_point.endswith("_after"):
                        original_start()
                    raise failure
                thread.start = failed_start
        return thread
    monkeypatch.setattr(client_module.PreviewRing, "create", create_preview)
    monkeypatch.setattr(client_module.multiprocessing, "get_context", lambda method: Context())
    monkeypatch.setattr(client_module.threading, "Thread", create_thread)
    try:
        with pytest.raises(RuntimeError) as caught:
            adapter.open_startup_streams((endpoint(),))
        assert caught.value is failure
        assert len(preview_rings) == 1
        assert all(pipe.closed for pipe in pipes)
        assert all(process.pid is None or not process.is_alive() for process in processes)
        assert all(not thread.is_alive() for thread in threads)
        for ring in preview_rings:
            assert all(stream.closed for stream in ring._streams.values())
            for handle in ring.bootstrap.streams:
                with pytest.raises(FileNotFoundError):
                    shared_memory.SharedMemory(name=handle.shm_name)
    finally:
        for pipe in pipes:
            if not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
        for process in processes:
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(1)
        for thread in threads:
            if thread.ident is not None:
                thread.join(1)
        for ring in preview_rings:
            if not all(stream.closed for stream in ring._streams.values()):
                ring.close(1)
