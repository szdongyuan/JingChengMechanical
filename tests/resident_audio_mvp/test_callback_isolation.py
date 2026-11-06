"""Regression tests for the bounded PortAudio callback boundary."""

from __future__ import annotations

import statistics
import threading
import time

import numpy as np

from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendFault,
    CaptureOutcomeStatus,
    ResidentStreamSpec,
)
from app.service.analysis.resident_audio_mvp.pool import ResidentAudioPool
from app.service.analysis.resident_audio_mvp.ingress import _SpscIngress
from app.service.analysis.resident_audio_mvp.ring import _ResidentRing
from app.service.analysis.resident_audio_mvp.stream import _ResidentStreamSlot
from tests.resident_audio_mvp.fakes import FakeBackend, ManualMonotonicClock


def stream_spec(hardware_id: str = "mic-1") -> ResidentStreamSpec:
    return ResidentStreamSpec(
        hardware_id=hardware_id,
        device_index=1,
        device_name=hardware_id,
        hostapi_index=0,
        hostapi_name="fake",
        sample_rate_hz=48_000,
        input_channel_count=2,
        capture_channel_indices=(0, 1),
        sample_format="float32",
        frames_per_buffer=2_400,
    )


def make_slot(
    hardware_id: str = "mic-1",
    *,
    decision_lock: object | None = None,
) -> tuple[_ResidentStreamSlot, ManualMonotonicClock]:
    clock = ManualMonotonicClock()
    slot = _ResidentStreamSlot(
        stream_spec(hardware_id),
        ring_capacity_frames=48_000 * 2 + 2_400,
        monotonic_ns=clock,
        decision_lock=decision_lock,
    )
    return slot, clock


def callback_block() -> np.ndarray:
    return np.zeros((2_400, 2), dtype=np.float32)


def test_callback_does_not_wait_for_pool_decision_lock() -> None:
    decision_lock = threading.Lock()
    slot, clock = make_slot(decision_lock=decision_lock)
    slot.publish_drain_owner()
    callback, _ = slot.backend_entrypoints()

    decision_lock.acquire()
    try:
        started = time.perf_counter_ns()
        directive = callback(
            callback_block(),
            2_400,
            clock(),
            BackendCallbackStatus.OK,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        decision_lock.release()
        slot.stop_worker()
        slot.join_worker(1)

    assert directive is BackendCallbackDirective.CONTINUE
    assert elapsed_ms <= 10


def test_backend_fault_delivery_does_not_wait_for_pool_decision_lock() -> None:
    decision_lock = threading.Lock()
    slot, _clock = make_slot(decision_lock=decision_lock)
    slot.publish_drain_owner()
    _, on_fault = slot.backend_entrypoints()

    decision_lock.acquire()
    try:
        started = time.perf_counter_ns()
        on_fault(
            BackendFault(
                BackendCallbackStatus.BACKEND_FAULT,
                "native stream failed",
            )
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        decision_lock.release()

    assert elapsed_ms <= 10
    assert slot._fault is not None
    assert "native stream failed" in slot._fault.summary
    slot.stop_worker()
    assert slot.join_worker(1)


def test_ring_and_observer_work_run_on_drain_not_callback(
    monkeypatch,
) -> None:
    slot, clock = make_slot()
    callback_thread_id = threading.get_ident()
    append_threads: list[int] = []
    observer_threads: list[int] = []
    observed = threading.Event()
    original_append = _ResidentRing.append_ingress

    def recording_append(self, samples, *, callback_monotonic_ns=None):
        append_threads.append(threading.get_ident())
        return original_append(
            self,
            samples,
            callback_monotonic_ns=callback_monotonic_ns,
        )

    def observer(_samples, _frame_count, _callback_ns) -> None:
        observer_threads.append(threading.get_ident())
        observed.set()

    monkeypatch.setattr(_ResidentRing, "append_ingress", recording_append)
    slot.publish_drain_owner(observer=observer)
    callback, _ = slot.backend_entrypoints()

    directive = callback(
        callback_block(),
        2_400,
        clock(),
        BackendCallbackStatus.OK,
    )

    assert directive is BackendCallbackDirective.CONTINUE
    assert slot.callback_sequence == 1
    assert slot.callback_heartbeat_monotonic_ns == clock()
    assert observed.wait(1)
    slot.stop_worker()
    assert slot.join_worker(1)
    assert append_threads and observer_threads
    assert callback_thread_id not in append_threads
    assert callback_thread_id not in observer_threads


def test_blocked_drain_observer_does_not_delay_callback_heartbeat() -> None:
    slot, clock = make_slot()
    observer_entered = threading.Event()
    observer_release = threading.Event()

    def blocked_observer(_samples, _frame_count, _callback_ns) -> None:
        observer_entered.set()
        observer_release.wait(1)

    slot.publish_drain_owner(observer=blocked_observer)
    callback, _ = slot.backend_entrypoints()
    assert callback(
        callback_block(), 2_400, clock(), BackendCallbackStatus.OK
    ) is BackendCallbackDirective.CONTINUE
    assert observer_entered.wait(1)
    clock.advance_ns(50_000_000)

    started = time.perf_counter_ns()
    directive = callback(
        callback_block(), 2_400, clock(), BackendCallbackStatus.OK
    )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000

    assert directive is BackendCallbackDirective.CONTINUE
    assert elapsed_ms <= 10
    assert slot.callback_sequence == 2
    assert slot.callback_heartbeat_monotonic_ns == clock()
    observer_release.set()
    slot.stop_worker()
    assert slot.join_worker(1)


def test_drain_block_past_ingress_capacity_faults_without_silent_drop() -> None:
    slot, clock = make_slot()
    observer_entered = threading.Event()
    observer_release = threading.Event()

    def blocked_observer(_samples, _frame_count, _callback_ns) -> None:
        observer_entered.set()
        observer_release.wait(1)

    slot.publish_drain_owner(observer=blocked_observer)
    callback, _ = slot.backend_entrypoints()
    samples = callback_block()
    assert callback(
        samples, 2_400, clock(), BackendCallbackStatus.OK
    ) is BackendCallbackDirective.CONTINUE
    assert observer_entered.wait(1)
    for _ in range(slot._ingress.capacity_blocks - 1):
        clock.advance_ns(50_000_000)
        assert callback(
            samples, 2_400, clock(), BackendCallbackStatus.OK
        ) is BackendCallbackDirective.CONTINUE
    accepted_heartbeat = slot.callback_heartbeat_monotonic_ns
    clock.advance_ns(50_000_000)

    directive = callback(
        samples, 2_400, clock(), BackendCallbackStatus.OK
    )

    assert directive is BackendCallbackDirective.STOP
    assert slot._fault is not None
    assert "ingress_overflow" in slot._fault.summary
    assert slot._ingress.overflow_count == 1
    assert slot.callback_sequence == slot._ingress.capacity_blocks
    assert slot.callback_heartbeat_monotonic_ns == accepted_heartbeat
    observer_release.set()
    slot.stop_worker()
    assert slot.join_worker(1)


def test_replacing_only_raw_handle_cannot_revive_retired_callback() -> None:
    slot, clock = make_slot()
    slot.publish_drain_owner()
    callback, _ = slot.backend_entrypoints()
    slot.invalidate_generation_under_decision_lock("RETIRED")
    slot._handle = object()
    before = slot._ring.write_cursor

    directive = callback(
        callback_block(), 2_400, clock(), BackendCallbackStatus.OK
    )

    slot.stop_worker()
    assert slot.join_worker(1)
    assert directive is BackendCallbackDirective.STOP
    assert slot._ring.write_cursor == before


def test_real_backend_fault_reaches_pool_capture_owner() -> None:
    backend = FakeBackend()
    opened = ResidentAudioPool.open(
        [stream_spec()],
        backend=backend,
        max_capture_frames=4_800,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    pool = opened.pool
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=4_800,
    )

    backend.handles["mic-1"].fault(summary="native endpoint failed")

    outcome = completion.wait(1)
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.FAILED
    assert outcome.error is not None
    assert "native endpoint failed" in outcome.error.summary


def test_first_accepted_ingress_is_ready_while_ring_drain_is_blocked(
    monkeypatch,
) -> None:
    drain_entered = threading.Event()
    drain_release = threading.Event()
    original_peek = _SpscIngress.peek

    def blocked_peek(self):
        drain_entered.set()
        drain_release.wait(1)
        return original_peek(self)

    monkeypatch.setattr(_SpscIngress, "peek", blocked_peek)
    backend = FakeBackend()

    opened = ResidentAudioPool.open(
        [stream_spec()],
        backend=backend,
        max_capture_frames=4_800,
        startup_stream_timeout_s=0.05,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )

    assert drain_entered.wait(1)
    slot = opened.pool._slots["mic-1"]
    assert opened.ready_count == 1
    assert slot.ready
    assert slot.callback_sequence == 1
    assert slot.callback_heartbeat_monotonic_ns is not None
    assert not slot._first_block_drained_event.is_set()
    drain_release.set()
    assert slot._first_block_drained_event.wait(1)
    opened.pool.shutdown()


def test_capture_progress_precedes_blocking_observer_for_each_block(
    monkeypatch,
) -> None:
    observer_entered = threading.Event()
    observer_release = threading.Event()
    order: list[str] = []
    original_publish = _ResidentStreamSlot.publish_drain_owner
    original_progress = ResidentAudioPool._progress_binding_locked
    initial_callback_ns = 1_000_000_000

    def drain_observer(_samples, _frame_count, callback_ns) -> None:
        if callback_ns > initial_callback_ns:
            order.append("observer")
            observer_entered.set()
            observer_release.wait(1)

    def publish_with_observer(self, progress=None, observer=None):
        assert observer is None
        return original_publish(self, progress, observer=drain_observer)

    def recording_progress(self, slot, record):
        result = original_progress(self, slot, record)
        order.append(
            "terminal_progress"
            if record.binding.binding_id not in self._bindings
            else "progress"
        )
        return result

    monkeypatch.setattr(
        _ResidentStreamSlot,
        "publish_drain_owner",
        publish_with_observer,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "_progress_binding_locked",
        recording_progress,
    )
    backend = FakeBackend()
    opened = ResidentAudioPool.open(
        [stream_spec()],
        backend=backend,
        max_capture_frames=2_400,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    pool = opened.pool
    binding = pool.reserve_capture("run-observer", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=2_400,
    )
    deadline = time.monotonic() + 1
    while not order and time.monotonic() < deadline:
        time.sleep(0.001)
    assert order
    order.clear()
    backend.clock.advance_ns(50_000_000)

    assert (
        backend.handles["mic-1"].emit_valid()
        is BackendCallbackDirective.CONTINUE
    )
    assert observer_entered.wait(1)

    outcome = completion.result_if_ready()
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.COMPLETED
    assert order[:2] == ["terminal_progress", "observer"]
    observer_release.set()
    pool.shutdown()


def test_six_stream_callback_timing_stays_within_bounded_gate() -> None:
    slots = [make_slot(f"mic-{number}")[0] for number in range(6)]
    callbacks = []
    for slot in slots:
        slot.publish_drain_owner()
        callbacks.append(slot.backend_entrypoints()[0])
    samples = callback_block()
    durations_ms: list[float] = []
    for _ in range(20):
        for callback in callbacks:
            started = time.perf_counter_ns()
            assert callback(
                samples,
                2_400,
                1_000_000_000,
                BackendCallbackStatus.OK,
            ) is BackendCallbackDirective.CONTINUE
            durations_ms.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
    for slot in slots:
        slot.stop_worker()
        assert slot.join_worker(1)

    ordered = sorted(durations_ms)
    p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
    assert p99 <= 5, statistics.quantiles(durations_ms, n=100)
    assert max(durations_ms) <= 10
