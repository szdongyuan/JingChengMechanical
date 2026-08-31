"""Supported-scale stress and the public shared-integration handoff gate."""

from __future__ import annotations

import ast
import gc
from pathlib import Path
import sys
import threading
import time
import weakref

import numpy as np
import pytest

import app.service.analysis.resident_audio_mvp.pool as pool_module
from app.service.analysis.resident_audio_mvp import (
    CancelStatus,
    CaptureOutcomeStatus,
    DiagnosticReason,
    DiagnosticStage,
    LifecycleFuture,
    LifecycleOutcome,
    LifecycleStatus,
    ReadinessTicket,
    ResidentDiagnostic,
    ResidentAudioPool,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
    SoundDeviceAudioBackend,
)
from app.service.analysis.resident_audio_mvp.stream import _ResidentStreamSlot
from tests.resident_audio_mvp.fakes import FakeBackend


def stream_spec(number: int) -> ResidentStreamSpec:
    return ResidentStreamSpec(
        hardware_id=f"mic-{number}",
        device_index=number,
        device_name=f"Microphone {number}",
        hostapi_index=0,
        hostapi_name="fake",
        sample_rate_hz=1_000,
        input_channel_count=2,
        capture_channel_indices=(0, 1),
        sample_format="float32",
        frames_per_buffer=10,
    )


def open_six() -> tuple[ResidentAudioPool, FakeBackend]:
    backend = FakeBackend()
    opened = ResidentAudioPool.open(
        [stream_spec(number) for number in range(1, 7)],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 6
    return opened.pool, backend


def test_six_by_six_concurrent_callbacks_finish_without_permit_or_record_leak(
) -> None:
    pool, backend = open_six()
    completions = []
    for number in range(1, 7):
        binding = pool.reserve_capture(f"run-{number}", f"mic-{number}")
        completions.append(
            pool.start_capture(
                binding,
                trigger_monotonic_ns=backend.clock(),
                target_frame_count=10,
            )
        )
    barrier = threading.Barrier(7)
    callback_time_ns = backend.clock() + 10_000_000
    threads = [
        threading.Thread(
            target=lambda handle=backend.handles[f"mic-{number}"]: (
                barrier.wait(),
                handle.emit(
                    np.ones((10, 2), dtype=np.float32),
                    callback_monotonic_ns=callback_time_ns,
                ),
            )
        )
        for number in range(1, 7)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()

    outcomes = [completion.wait(1) for completion in completions]
    assert all(outcome is not None for outcome in outcomes)
    assert all(
        outcome.status is CaptureOutcomeStatus.COMPLETED
        and outcome.audio is not None
        and not outcome.audio.samples.flags.writeable
        for outcome in outcomes
    )
    assert pool._capture_permits == 0
    assert pool._bindings == {}
    assert pool._run_ids == {}


def test_repeated_terminal_families_leave_only_caller_owned_outcomes() -> None:
    pool, backend = open_six()
    outcomes = []
    for number in range(1, 4):
        binding = pool.reserve_capture(f"run-complete-{number}", "mic-1")
        completion = pool.start_capture(
            binding,
            trigger_monotonic_ns=backend.clock(),
            target_frame_count=10,
        )
        backend.handles["mic-1"].emit_numbered_frames(
            first=number * 10,
            count=10,
        )
        outcomes.append(completion.wait(1))
        assert pool._capture_permits == 0
        assert pool._bindings == {}
    for number in range(1, 4):
        binding = pool.reserve_capture(f"run-cancel-{number}", "mic-1")
        outcomes.append(pool.cancel_capture(binding).outcome)
        assert pool._capture_permits == 0
        assert pool._bindings == {}
    assert all(outcome is not None for outcome in outcomes)
    assert not hasattr(pool, "_terminal_bindings")
    assert not hasattr(pool, "_cleanup_queue")


def test_callback_stress_keeps_ring_storage_and_metadata_bounded() -> None:
    pool, backend = open_six()
    ring = pool._slots["mic-1"]._ring
    storage = ring._storage
    storage_bytes = storage.nbytes

    for _ in range(10_000):
        backend.handles["mic-1"].emit_valid()

    assert ring._storage is storage
    assert ring._storage.nbytes == storage_bytes
    assert len(ring._blocks) <= ring.capacity_frames // 10 + 1


def test_handoff_db_and_registration_failures_cancel_provisional_capacity() -> None:
    pool, _ = open_six()
    order: list[str] = []
    runs: dict[str, str] = {}
    coordinator: dict[str, object] = {}

    order.append("freeze")
    db_failure = pool.reserve_capture("run-db-fail", "mic-1")
    order.append("reserve")
    try:
        raise RuntimeError("durable commit rejected")
    except RuntimeError:
        cancelled = pool.cancel_capture(db_failure)
        order.append("cancel-db-failure")
    assert cancelled.status is CancelStatus.CANCELLED
    assert "run-db-fail" not in runs

    accepted = pool.reserve_capture("run-register-fail", "mic-1")
    order.append("reserve-accepted")
    runs["run-register-fail"] = "ACTIVE"
    order.append("commit-active")
    try:
        raise RuntimeError("coordinator registration failed")
    except RuntimeError:
        outcome = pool.cancel_capture(accepted).outcome
        assert outcome is not None
        runs["run-register-fail"] = outcome.status.value
        order.append("terminal-existing-run")

    assert runs["run-register-fail"] == "CANCELLED"
    assert coordinator == {}
    assert pool._capture_permits == 0
    assert order == [
        "freeze",
        "reserve",
        "cancel-db-failure",
        "reserve-accepted",
        "commit-active",
        "terminal-existing-run",
    ]


def test_index_precedes_start_and_synchronous_start_fault_is_observed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend = open_six()
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.capture_completion(binding)
    runs = {"run-1": "ACTIVE"}
    coordinator = {binding.binding_id: completion}
    observations: list[object] = []

    def mapping_failure(
        _slot: _ResidentStreamSlot,
        _trigger_monotonic_ns: int,
    ) -> object:
        raise ValueError("timeline mapping failed")

    monkeypatch.setattr(_ResidentStreamSlot, "map_trigger", mapping_failure)
    returned = pool.start_capture(
        binding,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=10,
    )
    assert returned is coordinator[binding.binding_id]
    outcome = returned.result_if_ready()
    observations.append(outcome)
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code is ResidentErrorCode.FAULT
    runs["run-1"] = outcome.status.value
    assert observations == [outcome]
    assert runs == {"run-1": "FAILED"}
    assert pool._capture_permits == 0


def test_cancelled_builder_is_collectible_and_not_captured_by_workers() -> None:
    pool, backend = open_six()
    binding = pool.reserve_capture("run-1", "mic-1")
    pool.start_capture(
        binding,
        trigger_monotonic_ns=backend.clock(),
        target_frame_count=20,
    )
    record = pool._bindings[binding.binding_id]
    builder = record.buffer
    assert builder is not None
    reference = weakref.ref(builder)
    pool.cancel_capture(binding)
    assert record.buffer is None
    owner = pool._slots["mic-1"]._drain_owner
    assert owner is not None
    assert not hasattr(owner, "buffer")
    del builder
    gc.collect()
    assert reference() is None


def test_public_exports_and_static_import_boundary() -> None:
    import app.service.analysis.resident_audio_mvp as public

    assert public.ResidentAudioPool is ResidentAudioPool
    assert public.SoundDeviceAudioBackend is SoundDeviceAudioBackend
    assert public.DiagnosticReason is DiagnosticReason
    assert public.DiagnosticStage is DiagnosticStage
    assert public.LifecycleFuture is LifecycleFuture
    assert public.LifecycleOutcome is LifecycleOutcome
    assert public.LifecycleStatus is LifecycleStatus
    assert public.ReadinessTicket is ReadinessTicket
    assert public.ResidentDiagnostic is ResidentDiagnostic
    assert not hasattr(public, "_ResidentStreamSlot")
    assert not hasattr(public, "_ResidentRing")
    assert not hasattr(public, "_CaptureCompletionPublisher")
    assert not hasattr(public, "_LifecyclePublisher")

    package = (
        Path(__file__).parents[2]
        / "app"
        / "service"
        / "analysis"
        / "resident_audio_mvp"
    )
    forbidden = {
        "app.service.analysis.resident_audio",
        "_leaf_kernel",
        "ledger",
        "seeded",
        "reopen",
        "artifact",
        "settlement",
        "pool_retry",
        "pool_reconcile",
        "global_completion_handler",
    }
    for source_path in package.glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports)
        assert not any(token in joined for token in forbidden), source_path


def test_mixed_six_by_six_terminal_families_have_one_publication_and_no_owner_leak(
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

    pool, backend = open_six()
    bindings = []
    completions = []
    counting_events = []
    for number in range(1, 7):
        binding = pool.reserve_capture(f"run-mixed-{number}", f"mic-{number}")
        completion = pool.start_capture(
            binding,
            trigger_monotonic_ns=backend.clock(),
            target_frame_count=10 if number == 1 else 20,
        )
        counting_event = CountingEvent()
        completion._event = counting_event
        bindings.append(binding)
        completions.append(completion)
        counting_events.append(counting_event)
    records = [pool._bindings[binding.binding_id] for binding in bindings]
    builders = [record.buffer for record in records]
    assert all(builder is not None for builder in builders)
    references = [weakref.ref(builder) for builder in builders]
    with pytest.raises(ResidentRequestError) as raised:
        pool.reserve_capture("run-seventh", "mic-1")
    assert raised.value.error.code is ResidentErrorCode.CAPACITY

    start = threading.Barrier(5)
    terminal_phase = threading.Barrier(4)
    failures: list[BaseException] = []
    shutdown_reports: list[object] = []

    def guarded(action: object) -> None:
        try:
            action()
        except BaseException as exc:
            failures.append(exc)

    def complete_first() -> None:
        start.wait()
        backend.handles["mic-1"].emit(
            np.ones((10, 2), dtype=np.float32),
            callback_monotonic_ns=backend.clock() + 10_000_000,
        )
        assert completions[0].wait(1) is not None
        terminal_phase.wait()

    def cancel_second() -> None:
        start.wait()
        assert (
            pool.cancel_capture(bindings[1]).status
            is CancelStatus.CANCELLED
        )
        terminal_phase.wait()

    def fault_third() -> None:
        start.wait()
        backend.handles["mic-3"].fault(summary="mixed device fault")
        assert completions[2].wait(1) is not None
        terminal_phase.wait()

    def shutdown_remaining() -> None:
        start.wait()
        terminal_phase.wait()
        shutdown_reports.append(pool.shutdown())

    actions = [
        complete_first,
        cancel_second,
        fault_third,
        shutdown_remaining,
    ]
    threads = [
        threading.Thread(target=lambda action=action: guarded(action))
        for action in actions
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert failures == []
    assert shutdown_reports[0].status.value in {"CLOSED", "NON_CLEAN"}

    outcomes = [completion.result_if_ready() for completion in completions]
    assert all(outcome is not None for outcome in outcomes)
    assert outcomes[0].status is CaptureOutcomeStatus.COMPLETED
    assert outcomes[0].audio is not None
    assert not outcomes[0].audio.samples.flags.writeable
    assert outcomes[1].status is CaptureOutcomeStatus.CANCELLED
    assert outcomes[2].status is CaptureOutcomeStatus.FAILED
    assert all(
        outcome.status is CaptureOutcomeStatus.CANCELLED
        for outcome in outcomes[3:]
    )
    assert [event.set_calls for event in counting_events] == [1] * 6
    assert pool._bindings == {}
    assert pool._run_ids == {}
    assert pool._capture_permits == 0
    assert all(record.buffer is None for record in records)
    for binding, outcome in zip(bindings, outcomes, strict=True):
        assert pool.cancel_capture(binding).outcome is outcome

    for slot in pool._slots.values():
        owner = slot._drain_owner
        if owner is None:
            continue
        assert not hasattr(owner, "buffer")
        assert not hasattr(owner, "queue")
        target = getattr(owner.thread, "_target", None)
        closure_values = (
            []
            if target is None or target.__closure__ is None
            else [cell.cell_contents for cell in target.__closure__]
        )
        assert not any(
            isinstance(value, np.ndarray) for value in closure_values
        )
        if owner.thread.ident is not None:
            worker_frame = sys._current_frames().get(owner.thread.ident)
            while worker_frame is not None:
                assert not any(
                    isinstance(value, np.ndarray)
                    for value in worker_frame.f_locals.values()
                )
                worker_frame = worker_frame.f_back

    builders.clear()
    gc.collect()
    assert all(reference() is None for reference in references)


def test_repeated_failed_outcome_preparation_releases_every_builder_and_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend = open_six()
    records = []
    references = []
    retained = []

    def fail_audio(**_kwargs: object) -> object:
        raise MemoryError("repeated immutable transfer failure")

    monkeypatch.setattr(pool_module, "CapturedAudio", fail_audio)
    for number in range(20):
        binding = pool.reserve_capture(f"run-failed-{number}", "mic-1")
        completion = pool.start_capture(
            binding,
            trigger_monotonic_ns=backend.clock(),
            target_frame_count=10,
        )
        record = pool._bindings[binding.binding_id]
        builder = record.buffer
        assert builder is not None
        records.append(record)
        references.append(weakref.ref(builder))
        del builder
        backend.handles["mic-1"].emit_numbered_frames(
            first=number * 10,
            count=10,
        )
        outcome = completion.wait(1)
        assert outcome is not None
        assert outcome.status is CaptureOutcomeStatus.FAILED
        assert outcome.audio is None
        assert pool.cancel_capture(binding).outcome is outcome
        retained.append((binding, completion, outcome))
        assert record.buffer is None
        assert pool._bindings == {}
        assert pool._run_ids == {}
        assert pool._capture_permits == 0

    gc.collect()
    assert all(reference() is None for reference in references)
    assert len(retained) == 20
    assert all(
        completion.result_if_ready() is outcome
        for _binding, completion, outcome in retained
    )
