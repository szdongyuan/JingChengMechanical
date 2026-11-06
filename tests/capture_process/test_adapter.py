from dataclasses import replace
import gc
import threading
import time
import weakref
from uuid import uuid4

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import contracts as c
from app.service.capture_process import adapter as bridge
from app.service.capture_process.client import CaptureProcessError
from app.service.managed_input_contracts import ManagedInputCloseStatus
from tests.capture_process.fake_backend import endpoint
from tests.capture_process.test_worker import make_adapter


def target(adapter):
    ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
    return adapter.capture_target(ready.endpoint.hardware_id, ready.generation)


def reserve(target, name=None):
    return target.pool.reserve_capture(name or uuid4().hex, target.resident_hardware_id)


def start(target, binding, frames=80):
    return target.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=frames)


def test_bulk_copy_and_preview_congestion_do_not_block_second_start_or_cancel(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = bridge.copy_completed_audio
    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(bridge, "copy_completed_audio", slow_copy)
    adapter = make_adapter()
    lock = None
    try:
        route = target(adapter)
        lock = adapter.client.preview.bootstrap.streams[0].lock
        lock.acquire()
        first = reserve(route)
        start(route, first)
        assert entered.wait(3)
        assert adapter.client.request("stats")["pending_count"] == 1
        second = reserve(route)
        before = time.monotonic()
        second_completion = start(route, second, 120)
        assert time.monotonic() - before < .5
        third = reserve(route)
        before = time.monotonic()
        cancelled = route.pool.cancel_capture(third)
        assert time.monotonic() - before < .5
        assert cancelled.outcome.binding is third
        assert cancelled.outcome is route.pool.capture_completion(third).result_if_ready()
        release.set()
        assert route.pool.capture_completion(first).wait(3).status is c.CaptureOutcomeStatus.COMPLETED
        assert second_completion.wait(3).status is c.CaptureOutcomeStatus.COMPLETED
    finally:
        release.set()
        if lock is not None:
            lock.release()
        adapter.close_all(2.0)


def test_forged_terminal_binding_and_cross_owner_tokens_are_rejected():
    adapter = make_adapter()
    other = make_adapter()
    try:
        route = target(adapter)
        other_route = target(other)
        binding = reserve(route)
        cancelled = route.pool.cancel_capture(binding)
        assert cancelled.status is c.CancelStatus.CANCELLED
        assert route.pool.cancel_capture(binding).outcome is cancelled.outcome
        with pytest.raises(c.ResidentRequestError):
            other_route.pool.capture_completion(binding)
        forged = object.__new__(c.CaptureBinding)
        for name in c.CaptureBinding.__dataclass_fields__:
            object.__setattr__(forged, name, getattr(binding, name))
        with pytest.raises(c.ResidentRequestError):
            route.pool.capture_completion(forged)
    finally:
        adapter.close_all(2.0)
        other.close_all(2.0)


def test_real_managed_port_preserves_binding_and_completion_identity():
    from app.service.multi_run_coordinator_mvp.managed_resident_capture_port import (
        ManagedResidentCapturePort, ManagedResidentCaptureRoute,
    )
    adapter = make_adapter()
    port = None
    try:
        ready = adapter.open_startup_streams((endpoint(),)).ready_streams[0]
        port = ManagedResidentCapturePort(adapter, (ManagedResidentCaptureRoute(
            "1", ready.endpoint.hardware_id, ready.generation),))
        reserved = port.reserve(str(uuid4()), "1")
        reservation = reserved.reservation
        entry = port._reservations[reservation.reservation_id]
        completion = entry.target.pool.capture_completion(entry.binding)
        received, wake = [], threading.Event()
        def completed(outcome):
            received.append(outcome)
            wake.set()
        port.register_completion(reservation, completed)
        assert entry.completion is completion
        port.start(reservation, time.monotonic_ns(), 80)
        assert wake.wait(3)
        outcome = completion.result_if_ready()
        assert outcome.binding is entry.binding and outcome.audio.binding is entry.binding
        assert received[0].reservation is reservation
        assert received[0].audio.interleaved_bytes == outcome.audio.samples.tobytes()
    finally:
        if port is not None:
            port.close_and_join(2.0)
        adapter.close_all(2.0)


def test_calibration_does_not_block_capture_controls_and_preserves_contract():
    adapter = make_adapter()
    results, errors = [], []
    try:
        route = target(adapter)
        ready = adapter.client.startup.ready_streams[0]
        def measure():
            try:
                results.append(adapter.measure_calibration(ready.endpoint.hardware_id, ready.generation, 1, 600))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=measure)
        thread.start()
        deadline = time.monotonic() + 2
        while adapter.snapshot(ready.endpoint.hardware_id).active_capture_count == 0 and thread.is_alive():
            assert time.monotonic() < deadline
            time.sleep(.01)
        snapshot = adapter.snapshot(ready.endpoint.hardware_id)
        assert snapshot.active_capture_count == 1
        assert not snapshot.target_resource_owned  # Existing adapter reports active bindings first.
        binding = reserve(route)
        before = time.monotonic()
        completion = start(route, binding)
        assert time.monotonic() - before < .5
        assert completion.wait(2).status is c.CaptureOutcomeStatus.COMPLETED
        thread.join(3)
        assert not errors and len(results) == 1
        assert results[0].generation == ready.generation
        assert results[0].captured_frames == 600 and results[0].channel_index == 1
        assert results[0].measured_level_db > 60
        with pytest.raises((ValueError, c.ResidentRequestError)):
            adapter.measure_calibration(ready.endpoint.hardware_id, ready.generation, 2, 80)
    finally:
        adapter.close_all(2.0)


def test_child_death_settles_capture_and_reports_nonclean_owned_shutdown():
    adapter = make_adapter()
    route = target(adapter)
    binding = reserve(route)
    completion = start(route, binding, 1500)
    adapter.client.process.terminate()
    adapter.client.process.join(2)
    outcome = completion.wait(2)
    assert outcome.status is c.CaptureOutcomeStatus.FAILED and outcome.binding is binding
    with pytest.raises(CaptureProcessError):
        adapter.snapshot(endpoint().hardware_id)
    before = time.monotonic()
    assert adapter.close_all(.5)[0].status is ManagedInputCloseStatus.FAILED
    assert time.monotonic() - before < 1


def test_count_credit_includes_reserved_active_and_unacknowledged_results(monkeypatch):
    release, entered = threading.Event(), threading.Event()
    original = bridge.copy_completed_audio
    def held_copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(bridge, "copy_completed_audio", held_copy)
    adapter = make_adapter()
    try:
        route = target(adapter)
        completed = reserve(route)
        start(route, completed)
        assert entered.wait(2)
        bindings = [reserve(route) for _ in range(5)]
        start(route, bindings[0], 1500)
        assert adapter.client.request("stats")["pending_count"] == 6
        with pytest.raises(c.ResidentRequestError) as error:
            reserve(route)
        assert error.value.error.code is c.ResidentErrorCode.CAPACITY
        release.set()
        assert route.pool.capture_completion(completed).wait(3).status is c.CaptureOutcomeStatus.COMPLETED
        wait_until(lambda: adapter.client.request("stats")["pending_count"] == 5)
        assert reserve(route) is not None
    finally:
        release.set()
        adapter.close_all(2.0)


def test_receipt_retry_is_idempotent_and_follows_owned_audio_construction(monkeypatch):
    adapter = make_adapter()
    try:
        route = target(adapter)
        original = adapter.client.request
        receipts = []
        def lose_first_receipt_reply(op, **kwargs):
            result = original(op, **kwargs)
            if op == "receipt":
                receipts.append(kwargs["descriptor"])
                if len(receipts) == 1:
                    raise TimeoutError("simulated lost receipt reply")
            return result
        monkeypatch.setattr(adapter.client, "request", lose_first_receipt_reply)
        binding = reserve(route)
        outcome = start(route, binding).wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.COMPLETED
        wait_until(lambda: len(receipts) == 2)
        assert len(receipts) == 2 and receipts[0] == receipts[1]
        assert adapter.client.request("stats")["pending_count"] == 0
        with pytest.raises(ValueError):
            outcome.audio.samples.setflags(write=True)
    finally:
        adapter.close_all(2.0)


@pytest.mark.parametrize("field,value", [("frames", 2**40), ("owner_id", "0" * 32), ("capture_id", "other")])
def test_malformed_completed_descriptor_fails_without_receipt(monkeypatch, field, value):
    adapter = make_adapter()
    original_event = adapter._on_event
    def corrupt(message):
        if message["kind"] == "capture" and "descriptor" in message:
            message["descriptor"][field] = value
        original_event(message)
    monkeypatch.setattr(adapter, "_on_event", corrupt)
    receipts = []
    try:
        route = target(adapter)
        request = adapter.client.request
        def record(op, **kwargs):
            if op == "receipt":
                receipts.append(kwargs)
            return request(op, **kwargs)
        monkeypatch.setattr(adapter.client, "request", record)
        binding = reserve(route)
        outcome = start(route, binding).wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.FAILED and outcome.binding is binding
        wait_until(lambda: adapter.client.request("stats")["pending_count"] == 0)
        assert receipts == []
    finally:
        adapter.close_all(2.0)


def test_cancel_racing_completion_returns_the_canonical_winning_outcome():
    adapter = make_adapter()
    try:
        route = target(adapter)
        for delay in (0, .01, .019, .022, .04):
            binding = reserve(route)
            completion = start(route, binding, 20)
            time.sleep(delay)
            result = route.pool.cancel_capture(binding)
            assert result.status in (c.CancelStatus.CANCELLED, c.CancelStatus.ALREADY_TERMINAL)
            assert result.outcome is completion.wait(2)
            assert result.outcome.binding is binding
    finally:
        adapter.close_all(2.0)


def test_byte_credit_rejects_conservative_reservation_before_start():
    from app.service.capture_process.protocol import WorkerConfig
    config = WorkerConfig(frames_per_buffer=20, max_capture_frames=2000,
                          max_retained_bytes=16000, startup_stream_timeout_s=1.0)
    adapter = make_adapter(config=config)
    try:
        route = target(adapter)
        held = reserve(route)
        with pytest.raises(c.ResidentRequestError) as failure:
            reserve(route)
        assert failure.value.error.code is c.ResidentErrorCode.CAPACITY
        assert adapter.client.request("stats")["reserved_bytes"] == 16000
        route.pool.cancel_capture(held)
        deadline = time.monotonic() + 1
        while adapter.client.request("stats")["pending_count"]:
            assert time.monotonic() < deadline
        assert reserve(route) is not None
    finally:
        adapter.close_all(2.0)


def test_close_while_parent_copy_is_blocked_is_bounded_and_settles_capture(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def held(*args, **kwargs):
        entered.set()
        release.wait(3)
        raise RuntimeError("copy released after close")
    monkeypatch.setattr(bridge, "copy_completed_audio", held)
    adapter = make_adapter()
    route = target(adapter)
    binding = reserve(route)
    completion = start(route, binding)
    assert entered.wait(2)
    before = time.monotonic()
    try:
        assert adapter.close_all(.5)[0].status is ManagedInputCloseStatus.FAILED
        assert time.monotonic() - before < 1
        assert not adapter.client.process.is_alive()
        assert completion.wait(.1).status is c.CaptureOutcomeStatus.FAILED
    finally:
        release.set()
        adapter._materializer.join(2)


def test_closed_adapter_cannot_spawn_new_child():
    adapter = make_adapter()
    assert adapter.close_all(.5) == ()
    try:
        with pytest.raises(ValueError, match="closed"):
            adapter.open_startup_streams((endpoint(),))
    finally:
        if adapter.client is not None:
            adapter.client.close(1.0)


def test_stale_remote_ids_are_rejected_without_releasing_credit():
    adapter = make_adapter()
    try:
        route = target(adapter)
        reserve(route)
        with pytest.raises(c.ResidentRequestError):
            adapter.client.request("start", capture_id="stale", trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=20)
        with pytest.raises(c.ResidentRequestError):
            adapter.client.request("cancel", capture_id="stale")
        assert adapter.client.request("stats")["pending_count"] == 1
    finally:
        adapter.close_all(2.0)


def test_many_caller_held_outcomes_do_not_grow_owner_bookkeeping():
    adapter = make_adapter(backend_options={"preview_failure": True})
    outcomes = []
    try:
        route = target(adapter)
        for _ in range(14):
            binding = reserve(route)
            completion = start(route, binding, 20)
            outcome = completion.wait(3)
            assert outcome.status is c.CaptureOutcomeStatus.COMPLETED
            outcomes.append(outcome)
            wait_until(lambda: adapter.client.request("stats")["pending_count"] == 0)
            assert adapter.client.request("stats") == {"pending_count": 0, "reserved_bytes": 0, "captures": 0}
            assert adapter._pool.bindings == {} and adapter._pool.tickets == {}
            assert adapter.client._pending == {}
        for outcome in outcomes:
            assert route.pool.capture_completion(outcome.binding).result_if_ready() is outcome
            assert route.pool.cancel_capture(outcome.binding).outcome is outcome
    finally:
        adapter.close_all(2.0)


def test_adapter_invalid_target_and_calibration_preserve_value_error_contract():
    adapter = make_adapter()
    try:
        route = target(adapter)
        generation = route.generation
        with pytest.raises(ValueError):
            adapter.capture_target(generation.hardware_id, replace(generation, stream_generation=2))
        for invalid in (True, "20", 20.0):
            with pytest.raises(ValueError):
                adapter.measure_calibration(generation.hardware_id, generation, 0, invalid)
    finally:
        adapter.close_all(2.0)


def test_terminal_start_still_validates_frozen_request_dimensions():
    adapter = make_adapter()
    try:
        route = target(adapter)
        binding = reserve(route)
        route.pool.cancel_capture(binding)
        with pytest.raises(c.ResidentRequestError):
            route.pool.start_capture(binding, trigger_monotonic_ns=time.monotonic_ns(), target_frame_count=0)
    finally:
        adapter.close_all(2.0)


@pytest.mark.parametrize("overrides,max_frames,code", [
    ({"input_channels": 65}, 2000, "unsupported_channel_count"),
    ({"sample_rate_hz": 1000.5}, 2000, "unsupported_sample_rate"),
    ({"sample_rate_hz": 384001.0}, 2000, "unsupported_sample_rate"),
    ({"sample_rate_hz": 384000.0, "input_channels": 64}, 2000, "unsupported_ring_layout"),
    ({"input_channels": 64}, 1048577, "unsupported_capture_layout"),
])
def test_mixed_unsupported_endpoints_preserve_ready_capture_and_failure_codes(overrides, max_frames, code):
    from app.service.capture_process.protocol import WorkerConfig
    config = WorkerConfig(frames_per_buffer=20, max_capture_frames=max_frames,
                          startup_stream_timeout_s=1.0)
    adapter = make_adapter(config=config)
    submitted = (replace(endpoint(2), **overrides), endpoint())
    try:
        startup = adapter.open_startup_streams(submitted)
        assert [ready.endpoint.hardware_id.value for ready in startup.ready_streams] == [1]
        assert [(item.hardware_id.value, item.error_code) for item in startup.failures] == [(2, code)]
        assert adapter.open_startup_streams(submitted) is startup
        assert adapter.client.limits.channel_counts == (2,)
        assert len(adapter.client.preview.bootstrap.streams) == 1
        route = adapter.capture_target(startup.ready_streams[0].endpoint.hardware_id,
                                       startup.ready_streams[0].generation)
        outcome = start(route, reserve(route)).wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.COMPLETED
        assert outcome.audio.samples.shape == (80, 2)
    finally:
        closed = adapter.close_all(2.0)
    assert [(item.hardware_id.value, item.status) for item in closed] == [
        (1, ManagedInputCloseStatus.CLOSED), (2, ManagedInputCloseStatus.NOT_OPEN)]


@pytest.mark.parametrize("empty", [False, True])
def test_all_unsupported_endpoints_need_no_child_and_freeze_startup_membership(monkeypatch, empty):
    from app.service.capture_process.protocol import WorkerConfig
    from app.service.managed_input_contracts import ResidentStreamGenerationStamp
    def forbidden_child(*args, **kwargs):
        raise AssertionError("all unsupported startup must not allocate child resources")
    monkeypatch.setattr(bridge, "CaptureProcessClient", forbidden_child)
    config = WorkerConfig(frames_per_buffer=20, max_capture_frames=1048577)
    adapter = make_adapter(config=config)
    submitted = () if empty else (
        replace(endpoint(4), input_channels=64),
        replace(endpoint(3), sample_rate_hz=384000.0, input_channels=64),
        replace(endpoint(2), sample_rate_hz=1000.5),
        replace(endpoint(1), input_channels=65),
    )
    startup = adapter.open_startup_streams(submitted)
    assert startup.ready_streams == ()
    assert [(item.hardware_id.value, item.error_code) for item in startup.failures] == ([] if empty else [
        (1, "unsupported_channel_count"), (2, "unsupported_sample_rate"),
        (3, "unsupported_ring_layout"), (4, "unsupported_capture_layout"),
    ])
    assert adapter.open_startup_streams(submitted) is startup
    assert adapter.client is None and not adapter._materializer.is_alive()
    assert adapter.snapshot(None).active_capture_count == 0
    assert not adapter.snapshot(endpoint().hardware_id).target_resource_owned
    with pytest.raises(ValueError):
        adapter.open_startup_streams((endpoint(),))
    generation = ResidentStreamGenerationStamp(endpoint().hardware_id, 1, 1)
    with pytest.raises(ValueError):
        adapter.capture_target(endpoint().hardware_id, generation)
    with pytest.raises(ValueError):
        adapter.measure_calibration(endpoint().hardware_id, generation, 0, 20)
    closed = adapter.close_all(.5)
    assert [item.hardware_id.value for item in closed] == ([] if empty else [1, 2, 3, 4])
    assert all(item.status is ManagedInputCloseStatus.NOT_OPEN for item in closed)


def test_unsupported_endpoint_stays_not_open_after_child_failure():
    adapter = make_adapter()
    adapter.open_startup_streams((endpoint(), replace(endpoint(2), input_channels=65)))
    adapter.client.process.terminate()
    adapter.client.process.join(2)
    closed = adapter.close_all(.5)
    assert [(item.hardware_id.value, item.status) for item in closed] == [
        (1, ManagedInputCloseStatus.FAILED), (2, ManagedInputCloseStatus.NOT_OPEN)]


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_zero_budget_owned_reap_fences_captures_and_releases_child_resources():
    from multiprocessing import shared_memory
    adapter = make_adapter()
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding, 1500)
        names = [stream.shm_name for stream in adapter.client.preview.bootstrap.streams]
        before = time.monotonic()
        assert adapter.reap_owned_capture_process(0.0) is False
        assert time.monotonic() - before < .8
        assert not adapter.client.process.is_alive()
        assert completion.wait(.5).status is c.CaptureOutcomeStatus.FAILED
        assert adapter._close_requested and adapter._stop.is_set()
        assert adapter.reap_owned_capture_process(0.0) is False
        for name in names:
            with pytest.raises(FileNotFoundError):
                shared_memory.SharedMemory(name=name)
    finally:
        adapter.close_all(2.0)


@pytest.mark.parametrize("timeout", [0.0, -1.0, True, 0, float("inf"), float("nan"), 60.01])
def test_public_close_all_keeps_strict_positive_timeout_contract(timeout):
    adapter = make_adapter()
    with pytest.raises(ValueError):
        adapter.close_all(timeout)
    assert not adapter._close_requested
    assert not adapter._stop.is_set()


def test_materializer_start_failure_reaps_child_and_preview(monkeypatch):
    from multiprocessing import shared_memory
    adapter = make_adapter()
    created = []
    original_client = bridge.CaptureProcessClient
    def remember_client(*args, **kwargs):
        client = original_client(*args, **kwargs)
        created.append(client)
        return client
    def failed_start():
        raise RuntimeError("materializer thread start failed")
    monkeypatch.setattr(bridge, "CaptureProcessClient", remember_client)
    monkeypatch.setattr(adapter._materializer, "start", failed_start)
    before = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="materializer thread start failed"):
            adapter.open_startup_streams((endpoint(),))
        assert time.monotonic() - before < 3.5
        assert len(created) == 1 and not created[0].process.is_alive()
        for stream in created[0].preview.bootstrap.streams:
            with pytest.raises(FileNotFoundError):
                shared_memory.SharedMemory(name=stream.shm_name)
    finally:
        for client in created:
            client.close(2.0)


def test_cancel_and_start_between_receipt_and_materializer_return_are_canonical(monkeypatch):
    adapter = make_adapter()
    received, release = threading.Event(), threading.Event()
    original = adapter._receipt
    def hold_after_receipt(descriptor):
        original(descriptor)
        received.set()
        assert release.wait(3)
    monkeypatch.setattr(adapter, "_receipt", hold_after_receipt)
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding)
        assert received.wait(3)
        assert adapter.client.request("stats")["captures"] == 0
        cancelled = route.pool.cancel_capture(binding)
        repeated = start(route, binding)
        release.set()
        outcome = completion.wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.COMPLETED
        assert cancelled.status is c.CancelStatus.ALREADY_TERMINAL
        assert cancelled.outcome is outcome and repeated is completion
    finally:
        release.set()
        adapter.close_all(2.0)


@pytest.mark.parametrize("operation", ["cancel", "start"])
def test_control_already_in_flight_when_receipt_retires_child_binding_is_canonical(monkeypatch, operation):
    adapter = make_adapter()
    copying, copy_release = threading.Event(), threading.Event()
    control_entered, control_release = threading.Event(), threading.Event()
    copied = bridge.copy_completed_audio
    def hold_copy(*args, **kwargs):
        copying.set()
        assert copy_release.wait(4)
        return copied(*args, **kwargs)
    monkeypatch.setattr(bridge, "copy_completed_audio", hold_copy)
    results, errors = [], []
    thread = None
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding)
        assert copying.wait(2)
        request = adapter.client.request
        def delayed_request(op, **kwargs):
            if op == operation:
                control_entered.set()
                assert control_release.wait(4)
            return request(op, **kwargs)
        monkeypatch.setattr(adapter.client, "request", delayed_request)
        def control():
            try:
                results.append(route.pool.cancel_capture(binding) if operation == "cancel" else start(route, binding))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=control)
        thread.start()
        assert control_entered.wait(2)
        copy_release.set()
        outcome = completion.wait(3)
        wait_until(lambda: request("stats")["captures"] == 0)
        control_release.set()
        thread.join(2)
        assert not errors and len(results) == 1
        if operation == "cancel":
            assert results[0].status is c.CancelStatus.ALREADY_TERMINAL
            assert results[0].outcome is outcome
        else:
            assert results[0] is completion
    finally:
        copy_release.set()
        control_release.set()
        if thread is not None:
            thread.join(2)
        adapter.close_all(2.0)


def test_repeated_copy_failures_release_child_credit_without_success_receipts(monkeypatch):
    adapter = make_adapter()
    def failed_copy(*args, **kwargs):
        raise RuntimeError("synthetic copy failure")
    monkeypatch.setattr(bridge, "copy_completed_audio", failed_copy)
    receipts = []
    try:
        route = target(adapter)
        request = adapter.client.request
        def record(op, **kwargs):
            if op == "receipt":
                receipts.append(kwargs)
            return request(op, **kwargs)
        monkeypatch.setattr(adapter.client, "request", record)
        for _ in range(8):
            binding = reserve(route)
            outcome = start(route, binding, 20).wait(3)
            assert outcome.status is c.CaptureOutcomeStatus.FAILED
            wait_until(lambda: request("stats")["pending_count"] == 0)
            assert request("stats")["reserved_bytes"] == 0
        assert receipts == [] and adapter.client.process.is_alive()
    finally:
        adapter.close_all(2.0)


@pytest.mark.parametrize("side", ["parent", "child"])
def test_parent_and_child_last_completed_pcm_are_collectable(monkeypatch, side):
    adapter = make_adapter(backend_options={"audit_pcm_reclamation": True})
    intermediate_refs = []
    copy = bridge.copy_completed_audio
    def audited_copy(*args, **kwargs):
        pcm = copy(*args, **kwargs)
        intermediate_refs.append(weakref.ref(pcm))
        return pcm
    monkeypatch.setattr(bridge, "copy_completed_audio", audited_copy)
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding)
        outcome = completion.wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.COMPLETED
        samples = weakref.ref(outcome.audio.samples)
        binding = completion = outcome = None
        wait_until(lambda: adapter.client.request("stats")["captures"] == 0)
        def parent_collected():
            gc.collect()
            return samples() is None and all(ref() is None for ref in intermediate_refs)
        if side == "parent":
            wait_until(parent_collected)
        def child_collected():
            result = adapter.client.request("test_collect_pcm")
            assert result["observed"] == 1
            return result["alive"] == 0
        if side == "child":
            wait_until(child_collected)
    finally:
        adapter.close_all(2.0)


def test_calibration_of_unsupported_mixed_endpoint_keeps_value_error_and_credit():
    from app.service.managed_input_contracts import ResidentStreamGenerationStamp
    adapter = make_adapter()
    bad = replace(endpoint(2), input_channels=65)
    try:
        adapter.open_startup_streams((endpoint(), bad))
        with pytest.raises(ValueError, match="resident calibration target is not ready"):
            adapter.measure_calibration(bad.hardware_id, ResidentStreamGenerationStamp(bad.hardware_id, 1, 1), 0, 20)
        assert adapter.client.request("stats")["pending_count"] == 0
    finally:
        adapter.close_all(2.0)


@pytest.mark.parametrize("lost_after_receipt", [False, True])
def test_exhausted_receipt_retries_preserve_owned_audio_and_reap_child(monkeypatch, lost_after_receipt):
    adapter = make_adapter()
    attempts = []
    try:
        route = target(adapter)
        request = adapter.client.request
        def timeout_receipt(op, **kwargs):
            if op == "receipt":
                attempts.append(kwargs["descriptor"])
                if lost_after_receipt:
                    request(op, **kwargs)
                raise TimeoutError("simulated receipt timeout")
            return request(op, **kwargs)
        monkeypatch.setattr(adapter.client, "request", timeout_receipt)
        binding = reserve(route)
        completion = start(route, binding)
        outcome = completion.wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.COMPLETED
        wait_until(lambda: adapter.client._failure is not None)
        wait_until(lambda: not adapter.client.process.is_alive(), timeout=3)
        assert len(attempts) == 3 and attempts[0] == attempts[1] == attempts[2]
        assert completion.result_if_ready() is outcome
        assert route.pool.cancel_capture(binding).outcome is outcome
        np.testing.assert_array_equal(np.diff(outcome.audio.samples[:, 0]), np.ones(79))
    finally:
        adapter.close_all(2.0)


def test_failed_disposal_fences_and_reaps_child_without_a_success_receipt(monkeypatch):
    adapter = make_adapter()
    operations = []
    def failed_copy(*args, **kwargs):
        raise RuntimeError("synthetic copy failure")
    monkeypatch.setattr(bridge, "copy_completed_audio", failed_copy)
    try:
        route = target(adapter)
        request = adapter.client.request
        def failed_discard(op, **kwargs):
            operations.append(op)
            if op == "discard":
                raise RuntimeError("synthetic discard failure")
            return request(op, **kwargs)
        monkeypatch.setattr(adapter.client, "request", failed_discard)
        binding = reserve(route)
        outcome = start(route, binding).wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.FAILED
        wait_until(lambda: adapter.client._failure is not None)
        wait_until(lambda: not adapter.client.process.is_alive(), timeout=3)
        assert "discard" in operations and "receipt" not in operations
    finally:
        adapter.close_all(2.0)


def test_discard_authenticates_owner_and_rejects_active_capture(monkeypatch):
    adapter = make_adapter()
    copying, release = threading.Event(), threading.Event()
    copy = bridge.copy_completed_audio
    def held(*args, **kwargs):
        copying.set()
        assert release.wait(4)
        return copy(*args, **kwargs)
    monkeypatch.setattr(bridge, "copy_completed_audio", held)
    try:
        route = target(adapter)
        active = reserve(route)
        with pytest.raises(ValueError, match="published terminal"):
            adapter.client.request("discard", capture_id=active.binding_id, owner_id=adapter.client.owner_id)
        completed = reserve(route)
        completion = start(route, completed)
        assert copying.wait(3)
        with pytest.raises(ValueError, match="another capture owner"):
            adapter.client.request("discard", capture_id=completed.binding_id, owner_id="0" * 32)
        assert adapter.client.request("stats")["pending_count"] == 2
        release.set()
        assert completion.wait(3).status is c.CaptureOutcomeStatus.COMPLETED
        wait_until(lambda: adapter.client.request("stats")["pending_count"] == 1)
        assert not adapter.client.request("discard", capture_id=completed.binding_id, owner_id=adapter.client.owner_id)
    finally:
        release.set()
        adapter.close_all(2.0)


def test_terminal_race_does_not_mask_invalid_active_start_parameters(monkeypatch):
    adapter = make_adapter()
    invalid_received, release = threading.Event(), threading.Event()
    errors = []
    thread = None
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding, 300)
        request = adapter.client.request
        def delayed_error(op, **kwargs):
            try:
                return request(op, **kwargs)
            except c.ResidentRequestError:
                if op == "start":
                    invalid_received.set()
                    assert release.wait(3)
                raise
        monkeypatch.setattr(adapter.client, "request", delayed_error)
        def invalid_start():
            try:
                start(route, binding, 301)
            except c.ResidentRequestError as exc:
                errors.append(exc)
        thread = threading.Thread(target=invalid_start)
        thread.start()
        assert invalid_received.wait(2)
        assert completion.wait(3).status is c.CaptureOutcomeStatus.COMPLETED
        release.set()
        thread.join(2)
        assert len(errors) == 1 and errors[0].error.code is c.ResidentErrorCode.INVALID
    finally:
        release.set()
        if thread is not None:
            thread.join(2)
        adapter.close_all(2.0)


def test_failure_settles_capture_and_readiness_even_when_pipe_close_raises(monkeypatch):
    adapter = make_adapter()
    original_event = adapter._on_event
    def hold_readiness(message):
        if message["kind"] != "readiness":
            original_event(message)
    monkeypatch.setattr(adapter, "_on_event", hold_readiness)
    errors = []
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding, 1500)
        ticket = route.pool.begin_readiness(route.resident_hardware_id, time.monotonic_ns() + 2_000_000_000)
        close = adapter.client.connection.close
        def broken_close():
            raise OSError(6, "invalid Windows handle")
        monkeypatch.setattr(adapter.client.connection, "close", broken_close)
        try:
            adapter.client._fail(CaptureProcessError("synthetic disconnect"))
        except OSError as exc:
            errors.append(exc)
        monkeypatch.setattr(adapter.client.connection, "close", close)
        adapter.client._fail(CaptureProcessError("retry settlement"))
        assert completion.wait(.1) is not None
        assert completion.result_if_ready().status is c.CaptureOutcomeStatus.FAILED
        assert ticket.future.wait(.1).status is c.LifecycleStatus.RECOVERY_FAILED
        assert errors == []
    finally:
        adapter.close_all(2.0)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_concurrent_close_and_failure_serialize_pipe_close_and_finish_cleanup(monkeypatch):
    from multiprocessing import shared_memory
    adapter = make_adapter()
    entered, release = threading.Event(), threading.Event()
    errors, threads = [], []
    calls, active, maximum_active = 0, 0, 0
    counts_lock = threading.Lock()
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding, 1500)
        close = adapter.client.connection.close
        def racing_close():
            nonlocal calls, active, maximum_active
            with counts_lock:
                calls += 1
                active += 1
                maximum_active = max(maximum_active, active)
            entered.set()
            try:
                assert release.wait(2)
                close()
                raise OSError(6, "invalid Windows handle")
            finally:
                with counts_lock:
                    active -= 1
        monkeypatch.setattr(adapter.client.connection, "close", racing_close)
        def run(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run, args=(lambda: adapter.client._fail(CaptureProcessError("first failure")),))
        threads.append(first)
        first.start()
        assert entered.wait(1)
        for action in (lambda: adapter.client.close(.8),
                       lambda: adapter.client._fail(CaptureProcessError("second failure"))):
            thread = threading.Thread(target=run, args=(action,))
            threads.append(thread)
            thread.start()
        release.set()
        for thread in threads:
            thread.join(3)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert calls == 1 and maximum_active == 1
        assert completion.wait(.1).status is c.CaptureOutcomeStatus.FAILED
        assert not adapter.client.process.is_alive()
        assert not adapter.client._receiver.is_alive() and not adapter.client._sender.is_alive()
        for stream in adapter.client.preview.bootstrap.streams:
            with pytest.raises(FileNotFoundError):
                shared_memory.SharedMemory(name=stream.shm_name)
        assert adapter.client.close(.2) is False
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
        if adapter.client is not None:
            monkeypatch.setattr(adapter.client.connection, "close", close)
        adapter.close_all(2.0)


@pytest.mark.parametrize("corruption", ["rate", "frames", "channel_indices", "both_rates", "both_channels"])
def test_inconsistent_completed_metadata_is_discarded_before_copy(monkeypatch, corruption):
    adapter = make_adapter()
    on_event = adapter._on_event
    copied = []
    original_copy = bridge.copy_completed_audio
    def record_copy(*args, **kwargs):
        copied.append(True)
        return original_copy(*args, **kwargs)
    monkeypatch.setattr(bridge, "copy_completed_audio", record_copy)
    def corrupt(message):
        if message["kind"] == "capture" and "descriptor" in message:
            audio, descriptor = message["audio"], message["descriptor"]
            if corruption == "rate":
                audio["sample_rate_hz"] = 2000
            elif corruption == "frames":
                audio["frame_count"] -= 1
            elif corruption == "channel_indices":
                audio["channel_indices"] = (1, 2)
            elif corruption == "both_rates":
                audio["sample_rate_hz"] = descriptor["sample_rate_hz"] = 2000
            else:
                descriptor["channels"] = 1
                descriptor["nbytes"] = descriptor["frames"] * 4
                audio["channel_indices"] = (0,)
        on_event(message)
    monkeypatch.setattr(adapter, "_on_event", corrupt)
    try:
        route = target(adapter)
        binding = reserve(route)
        outcome = start(route, binding).wait(3)
        assert outcome.status is c.CaptureOutcomeStatus.FAILED
        wait_until(lambda: adapter.client.request("stats")["pending_count"] == 0)
        assert copied == []
    finally:
        adapter.close_all(2.0)


def test_real_calibration_failure_preserves_runtime_error_in_local_and_remote_adapters():
    from app.service.managed_input_resident_adapter import ResidentManagedInputAdapter
    from tests.capture_process.fake_backend import create_backend
    endpoints = (endpoint(),)
    local = ResidentManagedInputAdapter(
        backend=create_backend(endpoints, {"fault_after_frames": 100}),
        level_meter=lambda samples, rate: 0.0, frames_per_buffer=20,
        max_capture_frames=2000, startup_stream_timeout_s=1.0,
        fault_close_timeout_s=1.0, shutdown_timeout_s=2.0, calibration_wait_margin_s=1.0,
    )
    remote = make_adapter(backend_options={"fault_after_frames": 100})
    for adapter in (local, remote):
        try:
            ready = adapter.open_startup_streams(endpoints).ready_streams[0]
            with pytest.raises(RuntimeError, match="resident calibration capture failed"):
                adapter.measure_calibration(ready.endpoint.hardware_id, ready.generation, 0, 600)
        finally:
            adapter.close_all(2.0)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_receiver_failure_with_pipe_close_error_still_settles_and_retries_cleanup(monkeypatch):
    adapter = make_adapter()
    try:
        route = target(adapter)
        binding = reserve(route)
        completion = start(route, binding, 1500)
        original_close, original_poll = adapter.client.connection.close, adapter.client.connection.poll
        def broken_close():
            raise OSError(6, "invalid Windows handle")
        def broken_poll(*args, **kwargs):
            raise OSError("synthetic receiver failure")
        monkeypatch.setattr(adapter.client.connection, "close", broken_close)
        monkeypatch.setattr(adapter.client.connection, "poll", broken_poll)
        assert completion.wait(1).status is c.CaptureOutcomeStatus.FAILED
        adapter.client._receiver.join(1)
        assert not adapter.client._receiver.is_alive()
        monkeypatch.setattr(adapter.client.connection, "close", original_close)
        monkeypatch.setattr(adapter.client.connection, "poll", original_poll)
        assert adapter.client.close(.8) is False
        assert not adapter.client.process.is_alive()
        assert adapter.client.process.exitcode == 0  # Retried pipe close permits graceful child cleanup.
        assert not adapter.client._sender.is_alive()
        assert all(stream.closed for stream in adapter.client.preview._streams.values())
    finally:
        if adapter.client is not None:
            monkeypatch.setattr(adapter.client.connection, "close", original_close)
            monkeypatch.setattr(adapter.client.connection, "poll", original_poll)
        adapter.close_all(2.0)


@pytest.mark.parametrize("pause_at", ["client_constructor", "materializer_start"])
def test_close_overtaking_client_construction_prevents_ready_publication(monkeypatch, pause_at):
    adapter = make_adapter()
    created, opened, errors = [], [], []
    entered, release = threading.Event(), threading.Event()
    constructor = bridge.CaptureProcessClient
    def delayed_constructor(*args, **kwargs):
        client = constructor(*args, **kwargs)
        created.append(client)
        if pause_at == "client_constructor":
            entered.set()
            assert release.wait(3)
        return client
    monkeypatch.setattr(bridge, "CaptureProcessClient", delayed_constructor)
    if pause_at == "materializer_start":
        original_start = adapter._materializer.start
        def delayed_start():
            entered.set()
            assert release.wait(3)
            original_start()
        monkeypatch.setattr(adapter._materializer, "start", delayed_start)
    def open_adapter():
        try:
            opened.append(adapter.open_startup_streams((endpoint(),)))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=open_adapter)
    thread.start()
    try:
        assert entered.wait(3)
        before = time.monotonic()
        results = adapter.close_all(.3)
        assert time.monotonic() - before < .7
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not opened and len(errors) == 1
        adapter.close_all(.5)
        assert not created[0].process.is_alive()
        assert all(stream.closed for stream in created[0].preview._streams.values())
        assert not adapter._materializer.is_alive()
        assert results[0].status in (ManagedInputCloseStatus.FAILED, ManagedInputCloseStatus.TIMED_OUT)
        with pytest.raises(ValueError):
            adapter.open_startup_streams((endpoint(),))
    finally:
        release.set()
        thread.join(3)
        for client in created:
            client.close(2.0)


def test_nonclean_close_retries_unreleased_preview_without_changing_report(monkeypatch):
    adapter = make_adapter()
    try:
        target(adapter)
        original_close = adapter.client.preview.close
        calls = []
        def fail_once(timeout_s):
            calls.append(timeout_s)
            if len(calls) == 1:
                raise OSError("synthetic preview close failure")
            return original_close(timeout_s)
        monkeypatch.setattr(adapter.client.preview, "close", fail_once)
        assert adapter.client.close(.8) is False
        assert not all(stream.closed for stream in adapter.client.preview._streams.values())
        assert adapter.client.close(.8) is False
        assert all(stream.closed for stream in adapter.client.preview._streams.values())
        assert not adapter.client.process.is_alive()
    finally:
        if adapter.client is not None:
            original_close(1)
        adapter.close_all(1.0)


def test_close_before_first_client_resource_cancels_acquisition(monkeypatch):
    from app.service.capture_process import client as client_module
    adapter = make_adapter()
    entered, release = threading.Event(), threading.Event()
    opened, errors, allocated = [], [], []
    constructor = bridge.CaptureProcessClient
    create_preview = client_module.PreviewRing.create
    def delayed_constructor(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return constructor(*args, **kwargs)
    def record_preview(*args, **kwargs):
        allocated.append(True)
        return create_preview(*args, **kwargs)
    monkeypatch.setattr(bridge, "CaptureProcessClient", delayed_constructor)
    monkeypatch.setattr(client_module.PreviewRing, "create", record_preview)
    def opening():
        try:
            opened.append(adapter.open_startup_streams((endpoint(),)))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=opening)
    thread.start()
    try:
        assert entered.wait(2)
        adapter.close_all(.2)
        release.set()
        thread.join(2)
        assert not opened and len(errors) == 1
        assert not allocated and adapter.client is None
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(3)
        adapter.close_all(.5)


def test_concurrent_identical_startup_calls_share_one_owned_child(monkeypatch):
    adapter = make_adapter()
    entered, release, duplicate = threading.Event(), threading.Event(), threading.Event()
    created, opened, errors = [], [], []
    constructor = bridge.CaptureProcessClient
    def held_constructor(*args, **kwargs):
        if created:
            duplicate.set()
        client = constructor(*args, **kwargs)
        created.append(client)
        entered.set()
        assert release.wait(3)
        return client
    monkeypatch.setattr(bridge, "CaptureProcessClient", held_constructor)
    def opening():
        try:
            opened.append(adapter.open_startup_streams((endpoint(),)))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=opening) for _ in range(2)]
    try:
        threads[0].start()
        assert entered.wait(3)
        threads[1].start()
        assert not duplicate.wait(.1)
        release.set()
        for thread in threads:
            thread.join(3)
        assert not errors and len(created) == 1
        assert len(opened) == 2 and opened[0] is opened[1]
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(3)
        adapter.close_all(2.0)
        for client in created:
            client.close(1.0)
