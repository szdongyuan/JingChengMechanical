"""Cancel, fault quarantine, maintenance, and shutdown acceptance tests."""

from __future__ import annotations

import gc
import threading
import time
import weakref

import numpy as np
import pytest

import app.service.analysis.resident_audio_mvp.pool as pool_module
from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseResult,
    BackendCloseStatus,
    CancelStatus,
    CaptureBinding,
    CaptureCompletion,
    CaptureOutcomeStatus,
    MaintenanceStatus,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
    ShutdownStatus,
)
from app.service.analysis.resident_audio_mvp.pool import ResidentAudioPool
from tests.resident_audio_mvp.fakes import FakeBackend, ManualMonotonicClock


def stream_spec(hardware_id: str = "mic-1") -> ResidentStreamSpec:
    return ResidentStreamSpec(
        hardware_id=hardware_id,
        device_index=int(hardware_id.removeprefix("mic-") or "1"),
        device_name=hardware_id,
        hostapi_index=0,
        hostapi_name="fake",
        sample_rate_hz=1_000,
        input_channel_count=2,
        capture_channel_indices=(0, 1),
        sample_format="float32",
        frames_per_buffer=10,
    )


def open_pool(
    hardware_ids: tuple[str, ...] = ("mic-1",),
    *,
    backend: FakeBackend | None = None,
) -> tuple[ResidentAudioPool, FakeBackend]:
    selected_backend = backend or FakeBackend()
    opened = ResidentAudioPool.open(
        [stream_spec(hardware_id) for hardware_id in hardware_ids],
        backend=selected_backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == len(hardware_ids)
    return opened.pool, selected_backend


def malformed_same_owner_binding(
    valid: CaptureBinding,
    *,
    binding_id: str = "binding-99999999",
) -> CaptureBinding:
    token = object.__new__(CaptureBinding)
    object.__setattr__(token, "binding_id", binding_id)
    object.__setattr__(token, "run_id", "run-stale")
    object.__setattr__(token, "hardware_id", valid.hardware_id)
    object.__setattr__(token, "_owner_nonce", valid._owner_nonce)
    object.__setattr__(token, "_completion", CaptureCompletion())
    return token


def test_cancel_matrix_uses_binding_completion_without_terminal_registry() -> None:
    pool, backend = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")

    with pytest.raises(ResidentRequestError) as raised:
        pool.cancel_capture(object())  # type: ignore[arg-type]
    assert raised.value.error.code is ResidentErrorCode.INVALID

    unknown = malformed_same_owner_binding(binding)
    invalid = pool.cancel_capture(unknown)
    assert invalid.status is CancelStatus.INVALID
    assert invalid.error is not None
    assert invalid.error.code is ResidentErrorCode.INVALID
    assert invalid.outcome is None

    other_pool, _ = open_pool()
    foreign = other_pool.reserve_capture("run-foreign", "mic-1")
    foreign_result = pool.cancel_capture(foreign)
    assert foreign_result.status is CancelStatus.INVALID
    assert foreign_result.error is not None
    assert foreign_result.outcome is None

    result = pool.cancel_capture(binding)
    assert result.status is CancelStatus.CANCELLED
    assert result.error is None
    assert result.outcome is binding._completion.result_if_ready()
    assert result.outcome is not None
    assert result.outcome.status is CaptureOutcomeStatus.CANCELLED
    assert result.outcome.audio is None
    assert binding.binding_id not in pool._bindings
    assert pool._capture_permits == 0
    assert not hasattr(pool, "_terminal_bindings")
    assert backend.handles["mic-1"].request_stop_calls == 0
    assert backend.handles["mic-1"].close_deadlines == []

    again = pool.cancel_capture(binding)
    assert again.status is CancelStatus.ALREADY_TERMINAL
    assert again.outcome is result.outcome
    replacement = pool.reserve_capture("run-2", "mic-1")
    assert replacement.binding_id != binding.binding_id


def test_active_cancel_detaches_builder_and_releases_exactly_once() -> None:
    pool, backend = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")
    now = time.monotonic_ns()
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=now,
        target_frame_count=20,
    )
    record = pool._bindings[binding.binding_id]
    builder = record.buffer
    assert builder is not None
    builder_reference = weakref.ref(builder)

    result = pool.cancel_capture(binding)

    assert result.status is CancelStatus.CANCELLED
    assert completion.result_if_ready() is result.outcome
    assert record.buffer is None
    assert binding.binding_id not in pool._bindings
    assert pool._capture_permits == 0
    assert backend.handles["mic-1"].close_deadlines == []
    del builder
    gc.collect()
    assert builder_reference() is None


def test_first_terminal_races_keep_one_outcome_and_one_permit_release() -> None:
    pool, backend = open_pool()

    completed = pool.reserve_capture("run-complete", "mic-1")
    first_block_time = backend.clock()
    completed_waiter = pool.start_capture(
        completed,
        trigger_monotonic_ns=first_block_time,
        target_frame_count=10,
    )
    backend.handles["mic-1"].emit_numbered_frames(first=10, count=10)
    completed_outcome = completed_waiter.wait(1)
    assert completed_outcome is not None
    assert completed_outcome.status is CaptureOutcomeStatus.COMPLETED
    assert pool.cancel_capture(completed).outcome is completed_outcome

    faulted = pool.reserve_capture("run-fault", "mic-1")
    fault_waiter = pool.start_capture(
        faulted,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=20,
    )
    assert (
        backend.handles["mic-1"].emit_status(
            BackendCallbackStatus.DEVICE_FAULT
        )
        is BackendCallbackDirective.STOP
    )
    fault_outcome = fault_waiter.wait(1)
    assert fault_outcome is not None
    assert fault_outcome.status is CaptureOutcomeStatus.FAILED
    assert pool.cancel_capture(faulted).outcome is fault_outcome
    assert pool._capture_permits == 0
    assert pool._bindings == {}
    assert pool._run_ids == {}

    cancel_pool, _ = open_pool()
    cancelled = cancel_pool.reserve_capture("run-cancel", "mic-1")
    cancelled_outcome = cancel_pool.cancel_capture(cancelled).outcome
    assert cancelled_outcome is not None
    assert cancelled_outcome.status is CaptureOutcomeStatus.CANCELLED

    assert cancel_pool._capture_permits == 0
    assert cancel_pool._bindings == {}
    assert cancel_pool._run_ids == {}


def test_shutdown_first_makes_later_start_return_same_terminal_completion() -> None:
    pool, _ = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.capture_completion(binding)

    report = pool.shutdown()

    outcome = completion.result_if_ready()
    assert report.status is ShutdownStatus.CLOSED
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.CANCELLED
    assert (
        pool.start_capture(
            binding,
            trigger_monotonic_ns=time.monotonic_ns(),
            target_frame_count=10,
        )
        is completion
    )
    assert pool.cancel_capture(binding).outcome is outcome


def test_fault_quarantines_only_one_hardware_and_closes_once() -> None:
    pool, backend = open_pool(("mic-1", "mic-2"))
    failed = pool.reserve_capture("run-failed", "mic-1")
    sibling = pool.reserve_capture("run-sibling", "mic-2")
    failed_completion = pool.start_capture(
        failed,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=20,
    )
    sibling_completion = pool.start_capture(
        sibling,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=10,
    )

    backend.handles["mic-1"].fault(summary="usb detached")

    failed_outcome = failed_completion.wait(1)
    assert failed_outcome is not None
    assert failed_outcome.status is CaptureOutcomeStatus.FAILED
    assert failed_outcome.error is not None
    assert failed_outcome.error.code is ResidentErrorCode.FAULT
    assert backend.handles["mic-1"].request_stop_calls == 1
    assert len(backend.handles["mic-1"].close_deadlines) == 1
    assert backend.handles["mic-2"].request_stop_calls == 0
    assert backend.handles["mic-2"].close_deadlines == []

    backend.handles["mic-2"].emit_numbered_frames(first=10, count=10)
    sibling_outcome = sibling_completion.wait(1)
    assert sibling_outcome is not None
    assert sibling_outcome.status is CaptureOutcomeStatus.COMPLETED
    assert pool.cancel_capture(failed).outcome is failed_outcome

    before = pool._slots["mic-1"]._dropped_callback_count
    assert (
        backend.handles["mic-1"].emit_valid()
        is BackendCallbackDirective.STOP
    )
    assert pool._slots["mic-1"]._dropped_callback_count == before + 1


def test_fault_close_exceptions_are_contained_without_retry() -> None:
    backend = FakeBackend(
        request_stop_errors={"mic-1": RuntimeError("stop secret")},
        close_errors={"mic-1": ValueError("close secret")},
    )
    pool, backend = open_pool(backend=backend)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=20,
    )

    backend.handles["mic-1"].fault()

    outcome = completion.wait(1)
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.FAILED
    assert backend.handles["mic-1"].request_stop_calls == 1
    assert len(backend.handles["mic-1"].close_deadlines) == 1
    assert len(pool._resource_errors) == 2
    assert "fault request_stop mic-1 RuntimeError" in pool._resource_errors[0].summary
    assert "fault close mic-1 ValueError" in pool._resource_errors[1].summary

    pool.shutdown()
    assert backend.handles["mic-1"].request_stop_calls == 1
    assert len(backend.handles["mic-1"].close_deadlines) == 1


def test_runtime_fault_close_deadline_is_frozen_at_fault_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(pool_module.time, "monotonic_ns", clock)
    backend = FakeBackend(clock=clock)
    pool, backend = open_pool(backend=backend)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=20,
    )
    pool._slots["mic-1"]._decision_lock = None
    fault_time_ns = clock()
    pool._lock.acquire()
    try:
        backend.handles["mic-1"].fault()
        clock.advance_seconds(0.05)
    finally:
        pool._lock.release()

    assert completion.wait(1) is not None
    assert backend.handles["mic-1"].close_deadlines == [
        fault_time_ns + 100_000_000
    ]


def test_worker_ordinary_exception_quarantines_only_its_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend = open_pool(("mic-1", "mic-2"))
    failed = pool.reserve_capture("run-failed", "mic-1")
    sibling = pool.reserve_capture("run-sibling", "mic-2")
    failed_completion = pool.start_capture(
        failed,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=20,
    )
    sibling_completion = pool.start_capture(
        sibling,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=10,
    )
    slot_type = type(pool._slots["mic-1"])
    original_ring_bounds = slot_type.ring_bounds
    _, write_cursor = original_ring_bounds(pool._slots["mic-1"])
    failed_record = pool._bindings[failed.binding_id]
    failed_record.start_cursor = write_cursor
    failed_record.next_cursor = write_cursor

    def fail_one_slot(slot: object) -> tuple[int, int]:
        if slot is pool._slots["mic-1"]:
            raise RuntimeError("worker cursor failure")
        return original_ring_bounds(slot)

    monkeypatch.setattr(slot_type, "ring_bounds", fail_one_slot)
    assert not pool._drain_stream("mic-1")

    failed_outcome = failed_completion.wait(1)
    assert failed_outcome is not None
    assert failed_outcome.status is CaptureOutcomeStatus.FAILED
    assert failed_outcome.error is not None
    assert "worker drain mic-1: RuntimeError" in failed_outcome.error.summary
    backend.handles["mic-2"].emit_numbered_frames(first=10, count=10)
    assert sibling_completion.wait(1).status is CaptureOutcomeStatus.COMPLETED


def test_copy_exception_faults_all_bindings_on_hardware_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AdvancingEvent:
        def __init__(self) -> None:
            self.event = threading.Event()
            self.set_calls = 0

        def set(self) -> None:
            self.set_calls += 1
            clock.advance_seconds(0.05)
            self.event.set()

        def wait(self, timeout: float | None = None) -> bool:
            return self.event.wait(timeout)

        def is_set(self) -> bool:
            return self.event.is_set()

    clock = ManualMonotonicClock()
    monkeypatch.setattr(pool_module.time, "monotonic_ns", clock)
    backend = FakeBackend(clock=clock)
    pool, backend = open_pool(("mic-1", "mic-2"), backend=backend)
    first = pool.reserve_capture("run-first", "mic-1")
    second = pool.reserve_capture("run-second", "mic-1")
    sibling = pool.reserve_capture("run-sibling", "mic-2")
    first_completion = pool.start_capture(
        first,
        trigger_monotonic_ns=clock(),
        target_frame_count=20,
    )
    second_completion = pool.start_capture(
        second,
        trigger_monotonic_ns=clock(),
        target_frame_count=20,
    )
    sibling_completion = pool.start_capture(
        sibling,
        trigger_monotonic_ns=clock(),
        target_frame_count=10,
    )
    advancing_event = AdvancingEvent()
    first_completion._event = advancing_event
    builders = [
        pool._bindings[first.binding_id].buffer,
        pool._bindings[second.binding_id].buffer,
    ]
    references = [weakref.ref(builder) for builder in builders]
    for binding in (first, second):
        record = pool._bindings[binding.binding_id]
        record.start_cursor = 0
        record.next_cursor = 0
    slot_type = type(pool._slots["mic-1"])
    original_copy_into = slot_type.copy_into

    def fail_target_hardware(slot: object, **kwargs: object) -> None:
        if slot is pool._slots["mic-1"]:
            raise RuntimeError("copy boundary failed")
        original_copy_into(slot, **kwargs)

    monkeypatch.setattr(slot_type, "copy_into", fail_target_hardware)
    fault_time_ns = clock()

    assert not pool._drain_stream("mic-1")

    first_outcome = first_completion.result_if_ready()
    second_outcome = second_completion.result_if_ready()
    assert first_outcome is not None
    assert second_outcome is not None
    assert first_outcome.status is CaptureOutcomeStatus.FAILED
    assert second_outcome.status is CaptureOutcomeStatus.FAILED
    assert first_outcome.error is second_outcome.error
    assert advancing_event.set_calls == 1
    assert pool._slots["mic-1"]._state == "FAULTED"
    assert pool._slots["mic-1"]._generation is None
    assert backend.handles["mic-1"].request_stop_calls == 1
    assert backend.handles["mic-1"].close_deadlines == [
        fault_time_ns + 100_000_000
    ]
    assert backend.handles["mic-2"].request_stop_calls == 0
    assert backend.handles["mic-2"].close_deadlines == []
    assert first.binding_id not in pool._bindings
    assert second.binding_id not in pool._bindings
    assert sibling.binding_id in pool._bindings
    builders.clear()
    gc.collect()
    assert all(reference() is None for reference in references)

    backend.handles["mic-2"].emit(
        np.ones((10, 2), dtype=np.float32),
        callback_monotonic_ns=fault_time_ns + 10_000_000,
    )
    sibling_outcome = sibling_completion.wait(1)
    assert sibling_outcome is not None
    assert sibling_outcome.status is CaptureOutcomeStatus.COMPLETED


def test_completed_outcome_preparation_failure_becomes_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=10,
    )

    def fail_audio(**_kwargs: object) -> object:
        raise MemoryError("immutable transfer failed")

    monkeypatch.setattr(pool_module, "CapturedAudio", fail_audio)
    backend.handles["mic-1"].emit_numbered_frames(first=10, count=10)

    outcome = completion.wait(1)
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.FAILED
    assert outcome.audio is None
    assert outcome.error is not None
    assert outcome.error.code is ResidentErrorCode.FAULT
    assert "outcome preparation MemoryError" in outcome.error.summary
    assert pool._capture_permits == 0


def test_cancel_outcome_construction_failure_becomes_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingEvent:
        def __init__(self) -> None:
            self.event = threading.Event()
            self.set_calls = 0

        def set(self) -> None:
            self.set_calls += 1
            self.event.set()

        def wait(self, timeout: float | None = None) -> bool:
            return self.event.wait(timeout)

        def is_set(self) -> bool:
            return self.event.is_set()

    pool, _ = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.capture_completion(binding)
    counting_event = CountingEvent()
    completion._event = counting_event
    original_outcome = pool_module.CaptureOutcome
    construction_calls = 0

    def fail_first_outcome(**kwargs: object) -> object:
        nonlocal construction_calls
        construction_calls += 1
        if construction_calls == 1:
            assert kwargs["status"] is CaptureOutcomeStatus.CANCELLED
            assert binding.binding_id in pool._bindings
            assert pool._capture_permits == 1
            assert completion.result_if_ready() is None
            raise RuntimeError("cancel outcome construction failed")
        return original_outcome(**kwargs)

    monkeypatch.setattr(pool_module, "CaptureOutcome", fail_first_outcome)

    result = pool.cancel_capture(binding)

    outcome = completion.wait(1)
    assert outcome is not None
    assert result.status is CancelStatus.ALREADY_TERMINAL
    assert result.outcome is outcome
    assert outcome.status is CaptureOutcomeStatus.FAILED
    assert outcome.audio is None
    assert outcome.error is not None
    assert outcome.error.code is ResidentErrorCode.FAULT
    assert "outcome construction RuntimeError" in outcome.error.summary
    assert construction_calls == 2
    assert counting_event.set_calls == 1
    assert binding.binding_id not in pool._bindings
    assert binding.run_id not in pool._run_ids
    assert pool._capture_permits == 0
    again = pool.cancel_capture(binding)
    assert again.status is CancelStatus.ALREADY_TERMINAL
    assert again.outcome is outcome
    assert construction_calls == 2
    assert counting_event.set_calls == 1


def test_maintenance_validation_and_priority_are_exact_and_read_only() -> None:
    not_confirmed = BackendCloseResult(
        BackendCloseStatus.NOT_CONFIRMED,
        ResidentError(ResidentErrorCode.NON_CLEAN, "native close not confirmed"),
    )
    backend = FakeBackend(close_results={"mic-1": not_confirmed})
    pool, backend = open_pool(("mic-1", "mic-2"), backend=backend)

    with pytest.raises(ResidentRequestError):
        pool.maintenance_readiness("mic-1")  # type: ignore[arg-type]
    with pytest.raises(ResidentRequestError):
        pool.maintenance_readiness(("mic-1", "mic-1"))
    with pytest.raises(ResidentRequestError):
        pool.maintenance_readiness(("unknown",))
    assert backend.handles["mic-1"].close_deadlines == []

    empty = pool.maintenance_readiness(())
    assert empty.status is MaintenanceStatus.READY
    assert empty.active_binding_ids == ()
    assert empty.owned_hardware_ids == ()

    target = pool.maintenance_readiness(("mic-2",))
    assert target.status is MaintenanceStatus.BUSY
    assert target.active_binding_ids == ()
    assert target.owned_hardware_ids == ("mic-2",)

    binding = pool.reserve_capture("run-1", "mic-1")
    active = pool.maintenance_readiness(())
    assert active.status is MaintenanceStatus.BUSY
    assert active.active_binding_ids == (binding.binding_id,)
    assert active.owned_hardware_ids == ()
    pool.cancel_capture(binding)

    fault_binding = pool.reserve_capture("run-fault", "mic-1")
    backend.handles["mic-1"].fault()
    assert pool.capture_completion(fault_binding).wait(1) is not None
    blocked = pool.maintenance_readiness(None)
    assert blocked.status is MaintenanceStatus.BUSY
    assert blocked.owned_hardware_ids == ("mic-1", "mic-2")
    assert backend.handles["mic-1"].request_stop_calls == 1
    assert len(backend.handles["mic-1"].close_deadlines) == 1


def test_maintenance_treats_ring_allocation_failure_as_never_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_slot(*_args: object, **_kwargs: object) -> object:
        raise MemoryError("ring allocation failed")

    monkeypatch.setattr(pool_module, "_ResidentStreamSlot", fail_slot)
    opened = ResidentAudioPool.open(
        [stream_spec("mic-1")],
        backend=FakeBackend(),
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 0
    assert opened.pool._slots == {}

    readiness = opened.pool.maintenance_readiness(("mic-1",))

    assert readiness.status is MaintenanceStatus.READY
    assert readiness.active_binding_ids == ()
    assert readiness.owned_hardware_ids == ()


def test_maintenance_treats_create_input_failure_as_never_owned() -> None:
    backend = FakeBackend(
        create_errors={"mic-1": OSError("device never opened")},
        emit_valid_by_default=False,
    )
    opened = ResidentAudioPool.open(
        [stream_spec("mic-1")],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 0
    assert opened.pool._slots["mic-1"]._handle is None

    readiness = opened.pool.maintenance_readiness(("mic-1",))

    assert readiness.status is MaintenanceStatus.READY
    assert readiness.active_binding_ids == ()
    assert readiness.owned_hardware_ids == ()


@pytest.mark.parametrize(
    "deadline",
    [True, False, "1", 1.0, 0, -1],
)
def test_shutdown_rejects_invalid_deadline_without_state_change(
    deadline: object,
) -> None:
    pool, backend = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")

    with pytest.raises(ResidentRequestError) as raised:
        pool.shutdown(deadline_monotonic_ns=deadline)  # type: ignore[arg-type]

    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert pool._state == "OPEN"
    assert pool._bindings[binding.binding_id].binding is binding
    assert pool._capture_permits == 1
    assert backend.handles["mic-1"].request_stop_calls == 0
    assert backend.handles["mic-1"].close_deadlines == []


def test_concurrent_shutdown_returns_immediate_in_progress_and_freezes_deadline(
) -> None:
    pool, _ = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")
    first_deadline = time.monotonic_ns() + 2_000_000_000
    second_deadline = first_deadline + 5_000_000_000
    owner_done = threading.Event()
    owner_report: list[object] = []
    pool._lock.acquire()
    try:
        owner = threading.Thread(
            target=lambda: (
                owner_report.append(
                    pool.shutdown(deadline_monotonic_ns=first_deadline)
                ),
                owner_done.set(),
            )
        )
        owner.start()
        with pool._shutdown_gate:
            assert pool._shutdown_owner

        progress = pool.shutdown(deadline_monotonic_ns=second_deadline)

        assert progress.status is ShutdownStatus.IN_PROGRESS
        assert progress.finished_monotonic_ns is None
        assert progress.deadline_monotonic_ns == first_deadline
        assert progress.operator_action == "等待 owning shutdown caller 的最终报告"
        assert progress.unclosed_hardware_ids == ("mic-1",)
        assert progress.unjoined_worker_ids == ("stream:mic-1",)
        assert progress.non_terminal_binding_ids == (binding.binding_id,)
        assert not owner_done.is_set()
    finally:
        pool._lock.release()
    assert owner_done.wait(1)
    owner.join()
    final = owner_report[0]
    assert final.status is ShutdownStatus.CLOSED
    assert pool.shutdown(deadline_monotonic_ns=second_deadline) is final


def test_shutdown_publishes_all_events_before_backend_close_and_uses_one_deadline(
) -> None:
    close_entered = threading.Event()
    close_release = threading.Event()
    backend = FakeBackend(
        close_controls={"mic-1": (close_entered, close_release)}
    )
    pool, backend = open_pool(("mic-1", "mic-2"), backend=backend)
    reserved = pool.reserve_capture("run-reserved", "mic-1")
    active = pool.reserve_capture("run-active", "mic-2")
    active_completion = pool.start_capture(
        active,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=20,
    )
    reserved_completion = pool.capture_completion(reserved)
    reports: list[object] = []
    owner_done = threading.Event()
    deadline = time.monotonic_ns() + 2_000_000_000
    owner = threading.Thread(
        target=lambda: (
            reports.append(pool.shutdown(deadline_monotonic_ns=deadline)),
            owner_done.set(),
        )
    )
    owner.start()
    assert close_entered.wait(1)

    for completion in (reserved_completion, active_completion):
        outcome = completion.result_if_ready()
        assert outcome is not None
        assert outcome.status is CaptureOutcomeStatus.CANCELLED
        assert outcome.audio is None
    assert pool._bindings == {}
    assert pool._capture_permits == 0
    assert not owner_done.is_set()

    close_release.set()
    assert owner_done.wait(1)
    owner.join()
    report = reports[0]
    assert report.status is ShutdownStatus.CLOSED
    assert report.deadline_monotonic_ns == deadline
    assert report.finished_monotonic_ns >= report.started_monotonic_ns
    assert report.operator_action == ""
    assert {
        tuple(handle.close_deadlines)
        for handle in backend.handles.values()
    } == {(deadline,)}


def test_shutdown_non_clean_is_frozen_and_closed_readiness_stays_closed() -> None:
    not_confirmed = BackendCloseResult(
        BackendCloseStatus.NOT_CONFIRMED,
        ResidentError(ResidentErrorCode.NON_CLEAN, "native close timed out"),
    )
    backend = FakeBackend(
        close_results={"mic-1": not_confirmed},
        request_stop_errors={"mic-2": RuntimeError("stop failed")},
        close_errors={"mic-2": ValueError("close failed")},
    )
    pool, backend = open_pool(("mic-1", "mic-2"), backend=backend)

    report = pool.shutdown()

    assert report.status is ShutdownStatus.NON_CLEAN
    assert report.unclosed_hardware_ids == ("mic-1", "mic-2")
    assert report.non_terminal_binding_ids == ()
    assert report.operator_action == "不要做相关 maintenance；停止并重启进程"
    assert [error.code for error in report.errors] == [
        ResidentErrorCode.NON_CLEAN,
        ResidentErrorCode.NON_CLEAN,
    ]
    assert "shutdown request_stop mic-2 RuntimeError" in report.errors[0].summary
    assert "shutdown close mic-2 ValueError" in report.errors[1].summary
    assert pool.shutdown() is report
    readiness = pool.maintenance_readiness(None)
    assert readiness.status is MaintenanceStatus.CLOSED
    assert readiness.owned_hardware_ids == ("mic-1", "mic-2")


def test_shutdown_finishes_backend_phase_before_signalling_any_worker() -> None:
    pool, backend = open_pool(("mic-1", "mic-2"))
    first_owner = pool._slots["mic-1"]._drain_owner
    assert first_owner is not None
    second_handle = backend.handles["mic-2"]
    original_close = second_handle.close

    def assert_worker_phase_has_not_started(
        deadline_monotonic_ns: int,
    ) -> BackendCloseResult:
        assert not first_owner.stop_event.is_set()
        return original_close(deadline_monotonic_ns)

    second_handle.close = assert_worker_phase_has_not_started  # type: ignore[method-assign]

    report = pool.shutdown()

    assert report.status is ShutdownStatus.CLOSED


def test_shutdown_reconciles_fault_close_that_finishes_during_worker_join() -> None:
    close_entered = threading.Event()
    close_release = threading.Event()
    join_entered = threading.Event()
    backend = FakeBackend(
        close_controls={"mic-1": (close_entered, close_release)}
    )
    pool, backend = open_pool(backend=backend)
    owner = pool._slots["mic-1"]._drain_owner
    assert owner is not None
    original_thread = owner.thread

    class ObservedThread:
        def join(self, timeout: float | None = None) -> None:
            join_entered.set()
            original_thread.join(timeout)

        def is_alive(self) -> bool:
            return original_thread.is_alive()

    backend.handles["mic-1"].fault()
    assert close_entered.wait(1)
    owner.thread = ObservedThread()  # type: ignore[assignment]
    reports: list[object] = []
    shutdown_done = threading.Event()
    deadline = time.monotonic_ns() + 2_000_000_000
    shutdown_owner = threading.Thread(
        target=lambda: (
            reports.append(pool.shutdown(deadline_monotonic_ns=deadline)),
            shutdown_done.set(),
        )
    )
    shutdown_owner.start()
    assert join_entered.wait(1)
    assert not shutdown_done.is_set()

    close_release.set()
    assert shutdown_done.wait(1)
    shutdown_owner.join()

    report = reports[0]
    close_result = pool._slots["mic-1"].close_result_snapshot()
    assert close_result.status is BackendCloseStatus.CLOSED
    assert time.monotonic_ns() < deadline
    assert backend.handles["mic-1"].request_stop_calls == 1
    assert len(backend.handles["mic-1"].close_deadlines) == 1
    assert report.unclosed_hardware_ids == ()
    assert report.status is ShutdownStatus.CLOSED


def test_failed_start_malformed_close_result_is_bounded_non_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(pool_module.time, "monotonic_ns", clock)

    def fail_start(_handle: object) -> None:
        raise OSError("native start failed")

    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": fail_start},
        close_results={"mic-1": object()},  # type: ignore[dict-item]
        emit_valid_by_default=False,
    )
    opened = ResidentAudioPool.open(
        [stream_spec("mic-1")],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 0
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_100_000_000]

    report = opened.pool.shutdown()

    close_result = opened.pool._slots["mic-1"].close_result_snapshot()
    assert type(close_result) is BackendCloseResult
    assert close_result.status is BackendCloseStatus.NOT_CONFIRMED
    assert close_result.error is not None
    assert close_result.error.code is ResidentErrorCode.NON_CLEAN
    assert "startup close result mic-1 TypeError" in close_result.error.summary
    assert report.status is ShutdownStatus.NON_CLEAN
    assert report.unclosed_hardware_ids == ("mic-1",)
    assert len(report.errors) == 1
    assert report.errors[0].code is ResidentErrorCode.NON_CLEAN
    assert "startup close result mic-1 TypeError" in report.errors[0].summary
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_100_000_000]


def test_failed_start_cleanup_exceptions_are_bounded_non_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(pool_module.time, "monotonic_ns", clock)

    def fail_start(_handle: object) -> None:
        raise OSError("native start failed")

    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": fail_start},
        request_stop_errors={"mic-1": RuntimeError("stop failed")},
        close_errors={"mic-1": ValueError("close failed")},
        emit_valid_by_default=False,
    )
    opened = ResidentAudioPool.open(
        [stream_spec("mic-1")],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.02,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 0
    assert opened.streams[0].error is not None
    assert "start input mic-1: OSError: native start failed" in (
        opened.streams[0].error.summary
    )
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_100_000_000]

    report = opened.pool.shutdown()

    assert report.status is ShutdownStatus.NON_CLEAN
    assert report.unclosed_hardware_ids == ("mic-1",)
    assert len(report.errors) == 2
    assert "startup request_stop mic-1 RuntimeError: stop failed" in (
        report.errors[0].summary
    )
    assert "startup close mic-1 ValueError: close failed" in (
        report.errors[1].summary
    )
    close_result = opened.pool._slots["mic-1"].close_result_snapshot()
    assert type(close_result) is BackendCloseResult
    assert close_result.status is BackendCloseStatus.NOT_CONFIRMED
    assert close_result.error is not None
    assert close_result.error.code is ResidentErrorCode.NON_CLEAN
    assert "startup close mic-1 ValueError: close failed" in (
        close_result.error.summary
    )
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_100_000_000]


def test_startup_fault_retains_close_ownership_until_start_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    runtime_claim_entered = threading.Event()
    close_done = threading.Event()
    original_claim = (
        pool_module._ResidentStreamSlot.claim_close_attempt_under_decision_lock
    )

    def observe_runtime_claim(slot: object) -> tuple[object, bool]:
        result = original_claim(slot)
        runtime_claim_entered.set()
        return result

    def fault_before_start_returns(handle: object) -> None:
        original_close = handle.close

        def observe_close(deadline_monotonic_ns: int) -> object:
            try:
                return original_close(deadline_monotonic_ns)
            finally:
                close_done.set()

        handle.close = observe_close
        handle.fault(summary="synchronous startup fault")
        assert runtime_claim_entered.wait(1)

    monkeypatch.setattr(pool_module.time, "monotonic_ns", clock)
    monkeypatch.setattr(
        pool_module._ResidentStreamSlot,
        "claim_close_attempt_under_decision_lock",
        observe_runtime_claim,
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": fault_before_start_returns},
        close_results={"mic-1": object()},  # type: ignore[dict-item]
        emit_valid_by_default=False,
    )
    real_started_ns = time.monotonic_ns()

    opened = ResidentAudioPool.open(
        [stream_spec("mic-1")],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.05,
        fault_close_timeout_s=0.02,
        shutdown_timeout_s=0.07,
    )

    assert close_done.wait(1)
    assert time.monotonic_ns() - real_started_ns < 1_000_000_000
    assert opened.ready_count == 0
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_070_000_000]
    close_result = opened.pool._slots["mic-1"].close_result_snapshot()
    assert type(close_result) is BackendCloseResult
    assert close_result.status is BackendCloseStatus.NOT_CONFIRMED
    assert close_result.error is not None
    assert "startup close result mic-1 TypeError" in close_result.error.summary

    report = opened.pool.shutdown()

    assert report.status is ShutdownStatus.NON_CLEAN
    assert report.unclosed_hardware_ids == ("mic-1",)
    assert len(report.errors) == 1
    assert "startup close result mic-1 TypeError" in report.errors[0].summary
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_070_000_000]


def test_shutdown_treats_create_input_failure_as_never_owned_and_clean() -> None:
    backend = FakeBackend(
        create_errors={"mic-1": OSError("device never opened")},
        emit_valid_by_default=False,
    )
    opened = ResidentAudioPool.open(
        [stream_spec("mic-1")],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 0
    assert "mic-1" not in backend.handles

    report = opened.pool.shutdown()

    assert report.status is ShutdownStatus.CLOSED
    assert report.unclosed_hardware_ids == ()
    assert report.errors == ()
    assert report.operator_action == ""


def test_shutdown_does_not_wait_for_slot_lock_after_pool_lock_acquisition() -> None:
    pool, _ = open_pool()
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.capture_completion(binding)
    decision_acquired = threading.Event()
    shutdown_done = threading.Event()
    reports: list[object] = []
    base_lock = pool._lock

    class ObservedLock:
        def __enter__(self) -> object:
            base_lock.acquire()
            decision_acquired.set()
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            base_lock.release()

    pool._lock = ObservedLock()  # type: ignore[assignment]
    slot_lock = pool._slots["mic-1"]._lock
    slot_lock.acquire()
    owner = threading.Thread(
        target=lambda: (
            reports.append(pool.shutdown()),
            shutdown_done.set(),
        )
    )
    owner.start()
    try:
        assert decision_acquired.wait(1)
        completed_without_slot_lock = shutdown_done.wait(0.1)
    finally:
        slot_lock.release()
    assert shutdown_done.wait(1)
    owner.join()

    assert completed_without_slot_lock
    assert reports[0].status is ShutdownStatus.CLOSED
    outcome = completion.result_if_ready()
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.CANCELLED
