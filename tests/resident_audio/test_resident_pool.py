from __future__ import annotations

import gc
import threading
import time
import weakref
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.service.analysis.resident_audio import (
    ArtifactState,
    CaptureArtifactStore,
    CaptureErrorCode,
    CaptureOwnerState,
    CaptureRequest,
    DesiredHardware,
    LeaseState,
    PoolState,
    ResolvedInputDevice,
    ResidentAudioPool,
    ResidentStream,
    StreamState,
)
from app.service.analysis.resident_audio.owner_finality import (
    OwnerFinalityState,
)
from app.service.analysis.resident_audio.owner_types import (
    OwnerControlEvent,
    OwnerControlKind,
)
from app.service.analysis.resident_audio.owner_identity import (
    FinalityAckReceipt,
    FinalityPruneOutcome,
    FinalityPruneReceipt,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerNonAdmissionReason,
    OwnerRemovalOutcome as ValueOwnerRemovalOutcome,
    OwnerRemovalReceipt as ValueOwnerRemovalReceipt,
)
from app.service.analysis.resident_audio.owner_settlement_types import (
    ArtifactSettlementResult,
    SettlementIntent,
)
from app.service.analysis.resident_audio.artifact_cleanup import (
    ArtifactCleanupReport,
)
from app.service.analysis.resident_audio.stream_types import (
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
)
from tests.resident_audio.fakes import (
    FakeInputHandle,
    ManualClock,
    ManualScheduler,
)


def device(
    hardware_id: str,
    *,
    index: int = 1,
    rate: float = 100.0,
    channels: int = 1,
) -> ResolvedInputDevice:
    return ResolvedInputDevice(
        hardware_id=hardware_id,
        device_index=index,
        name=f"Microphone {hardware_id}",
        host_api="ASIO",
        sample_rate=rate,
        channels=channels,
        resolution_version=1,
    )


class PoolResolver:
    def __init__(self) -> None:
        self.devices: dict[str, ResolvedInputDevice] = {}
        self.calls: list[tuple[str, int, int, object]] = []
        self.failures: dict[str, Exception] = {}
        self.started: dict[str, threading.Event] = defaultdict(threading.Event)
        self.releases: dict[str, threading.Event] = {}

    def resolve(self, hardware_id, desired_revision, resolution_attempt_id, cancel_token):
        self.calls.append(
            (hardware_id, desired_revision, resolution_attempt_id, cancel_token)
        )
        self.started[hardware_id].set()
        release = self.releases.get(hardware_id)
        if release is not None:
            assert release.wait(2.0)
        if hardware_id in self.failures:
            raise self.failures[hardware_id]
        return self.devices[hardware_id]


class MultiBackend:
    def __init__(self) -> None:
        self.handles: dict[str, list[FakeInputHandle]] = defaultdict(list)
        self.handle_setups = {}
        self.open_order: list[str] = []
        self.close_order: list[str] = []
        self.close_threads: list[str] = []
        self.open_started: dict[str, threading.Event] = defaultdict(threading.Event)
        self.open_releases: dict[str, threading.Event] = {}
        self.open_return_releases: dict[str, threading.Event] = {}
        self.failures: dict[str, Exception] = {}
        self.force_terminate_calls = 0

    def open_input(self, resolved, *, block_frames, callback):
        hardware_id = resolved.hardware_id
        self.open_started[hardware_id].set()
        release = self.open_releases.get(hardware_id)
        if release is not None:
            assert release.wait(2.0)
        if hardware_id in self.failures:
            raise self.failures[hardware_id]
        backend = self

        class TrackedHandle(FakeInputHandle):
            def close(self):
                super().close()
                backend.close_order.append(hardware_id)
                backend.close_threads.append(threading.current_thread().name)

        handle = TrackedHandle()
        handle.callback = callback
        setup = self.handle_setups.get(hardware_id)
        if setup is not None:
            setup(handle)
        self.handles[hardware_id].append(handle)
        self.open_order.append(hardware_id)
        return_release = self.open_return_releases.get(hardware_id)
        if return_release is not None:
            assert return_release.wait(2.0)
        return handle

    def force_terminate(self):
        self.force_terminate_calls += 1


def make_pool(
    tmp_path: Path,
    *,
    quota_bytes: int = 1_000_000,
    reconcile_ready_timeout: float = 2.0,
    stream_factory=None,
    terminal_diagnostic_limit: int = 256,
    retry_scheduler=None,
):
    resolver = PoolResolver()
    backend = MultiBackend()
    clock = ManualClock(10.0)
    store = CaptureArtifactStore(
        tmp_path,
        quota_bytes=quota_bytes,
        free_space_bytes=lambda _path: 10_000_000,
        minimum_free_bytes=0,
    )
    pool_kwargs = {}
    if stream_factory is not None:
        pool_kwargs["stream_factory"] = stream_factory
    if retry_scheduler is not None:
        pool_kwargs["retry_scheduler"] = retry_scheduler
    pool = ResidentAudioPool(
        resolver=resolver,
        backend=backend,
        artifact_store=store,
        monotonic_clock=clock,
        reconcile_ready_timeout=reconcile_ready_timeout,
        terminal_diagnostic_limit=terminal_diagnostic_limit,
        **pool_kwargs,
    )
    return pool, resolver, backend, store, clock


def test_pool_retry_is_per_hardware_fresh_capped_and_resets_only_after_ready(
    tmp_path,
):
    scheduler = ManualScheduler()
    pool, resolver, backend, _, clock = make_pool(
        tmp_path, retry_scheduler=scheduler
    )
    handle, _ = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")

    resolver.failures["mic-a"] = RuntimeError("temporarily unavailable")
    assert stream.report_backend_failure(RuntimeError("lost"), timeout=1.0)
    assert [call.delay for call in scheduler.calls] == [0.5]
    failed_diagnostic = pool.snapshot().stream_for("mic-a")
    first_failed_generation = failed_diagnostic.stream_generation
    assert failed_diagnostic.retry_attempt == 1
    assert failed_diagnostic.retry_delay == 0.5
    assert failed_diagnostic.exception_type == "RuntimeError"
    assert "lost" not in (failed_diagnostic.message or "")

    scheduler.calls[-1].run()
    assert len(resolver.calls) == 2
    assert resolver.calls[-1][2] == 2
    assert handle.physically_closed
    assert [call.delay for call in scheduler.calls] == [0.5, 1.0]

    for expected_delay in (2.0, 5.0, 10.0, 10.0):
        scheduler.calls[-1].run()
        assert [call.delay for call in scheduler.calls][-1] == expected_delay
    assert [call[2] for call in resolver.calls] == list(
        range(1, len(resolver.calls) + 1)
    )

    resolver.failures.pop("mic-a")
    scheduler.calls[-1].run()
    retry_handle = backend.handles["mic-a"][-1]
    retry_stream = pool.stream_for_testing("mic-a")
    assert retry_stream is not None
    assert retry_stream is not stream
    assert (
        pool.snapshot().stream_for("mic-a").stream_generation
        > first_failed_generation
    )

    # Merely opening a generation does not reset the backoff.
    assert retry_stream.report_backend_failure(
        RuntimeError("failed before readiness"), timeout=1.0
    )
    assert scheduler.calls[-1].delay == 10.0
    scheduler.calls[-1].run()
    ready_handle = backend.handles["mic-a"][-1]
    clock.advance(0.1)
    ready_handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert pool.synchronize_stream("mic-a", timeout=1.0)
    ready_stream = pool.stream_for_testing("mic-a")
    assert ready_stream.report_backend_failure(
        RuntimeError("failed after readiness"), timeout=1.0
    )
    assert scheduler.calls[-1].delay == 0.5
    pool.shutdown()


def test_hardware_churn_retires_retry_and_diagnostic_state(tmp_path):
    scheduler = ManualScheduler()
    pool, resolver, _, _, _ = make_pool(
        tmp_path, retry_scheduler=scheduler
    )

    for index in range(100):
        hardware_id = f"churn-{index}"
        resolver.failures[hardware_id] = RuntimeError("unavailable")
        assert pool.reconcile([desired(hardware_id)]).accepted
        assert hardware_id in pool._retry_backoffs
        assert hardware_id in pool._stream_diagnostics
        assert pool.reconcile([]).accepted
        assert not pool._scheduled_retries
        assert not pool._retry_backoffs
        assert not pool._stream_diagnostics

    assert len(scheduler.calls) == 100
    assert all(call.canceled for call in scheduler.calls)
    pool.shutdown()


def test_desired_removal_and_shutdown_cancel_retry_and_stale_task_cannot_publish(
    tmp_path,
):
    scheduler = ManualScheduler()
    pool, resolver, backend, _, _ = make_pool(
        tmp_path, retry_scheduler=scheduler
    )
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    assert stream.report_backend_failure(RuntimeError("lost"), timeout=1.0)
    stale_retry = scheduler.calls[-1]

    assert pool.reconcile([]).accepted
    assert stale_retry.canceled
    calls_before_stale = len(resolver.calls)
    stale_retry.run(even_if_canceled=True)
    assert len(resolver.calls) == calls_before_stale
    assert pool.snapshot().published_hardware_ids == ()

    resolver.devices["mic-a"] = device("mic-a", index=77)
    assert pool.reconcile([desired("mic-a")]).accepted
    replacement = pool.stream_for_testing("mic-a")
    assert replacement.report_backend_failure(RuntimeError("again"), timeout=1.0)
    shutdown_retry = scheduler.calls[-1]
    result = pool.shutdown()
    assert shutdown_retry.canceled
    calls_before_shutdown_stale = len(resolver.calls)
    shutdown_retry.run(even_if_canceled=True)
    assert len(resolver.calls) == calls_before_shutdown_stale
    assert result.state is PoolState.CLOSED
    assert pool.snapshot().published_hardware_ids == ()


def test_shutdown_deadline_is_captured_before_step_one_and_waiter_reuses_owner_result(
    tmp_path, monkeypatch
):
    pool, _, _, store, _ = make_pool(tmp_path)
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    waiter_started = threading.Event()
    clock_calls = []
    reports = []

    def shutdown_clock():
        clock_calls.append(
            (threading.current_thread().name, pool.snapshot().state)
        )
        return 100.0

    def blocked_cleanup():
        cleanup_entered.set()
        assert cleanup_release.wait(1.0)
        return ArtifactCleanupReport()

    monkeypatch.setattr(pool, "_shutdown_clock", shutdown_clock)
    monkeypatch.setattr(store, "begin_closing", blocked_cleanup)
    owner = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="shutdown-deadline-owner",
    )

    def wait_for_owner():
        waiter_started.set()
        reports.append(pool.shutdown())

    waiter = threading.Thread(
        target=wait_for_owner,
        name="shutdown-deadline-waiter",
    )
    owner.start()
    assert cleanup_entered.wait(1.0)
    waiter.start()
    assert waiter_started.wait(1.0)
    cleanup_release.set()
    owner.join(2.0)
    waiter.join(2.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert len(reports) == 2
    assert reports[0] is reports[1]
    assert reports[0].state is PoolState.CLOSED
    assert clock_calls[0] == ("shutdown-deadline-owner", PoolState.OPEN)
    assert all(
        thread_name != "shutdown-deadline-waiter"
        for thread_name, _state in clock_calls
    )


def test_shutdown_exact_phase_trace_and_blocked_physical_close_gate(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path / "ordered")
    handle, token = make_ready(pool, resolver, backend)
    pool.reserve("ordered-committed")
    assert pool.commit(
        "ordered-committed",
        token,
        request(token, "ordered-committed", duration=1.0),
    ).state is LeaseState.COMMITTED
    pool.reserve("ordered-reserved")
    pool.reserve("ordered-committing")
    allocation_entered = threading.Event()
    allocation_release = threading.Event()
    original_allocate = store.allocate_writer

    def blocked_allocate(**kwargs):
        if kwargs["run_id"] == "ordered-committing":
            allocation_entered.set()
            assert allocation_release.wait(1.0)
        return original_allocate(**kwargs)

    monkeypatch.setattr(store, "allocate_writer", blocked_allocate)
    commit_outcomes = []
    commit_worker = threading.Thread(
        target=lambda: commit_outcomes.append(
            pool.commit(
                "ordered-committing",
                token,
                request(token, "ordered-committing", duration=1.0),
            )
        ),
        name="resident-audio-commit-ordered-committing",
    )
    commit_worker.start()
    assert allocation_entered.wait(1.0)
    rollback_settled = pool._leases[
        "ordered-committing"
    ].pre_teardown_settled
    original_rollback_wait = rollback_settled.wait
    trace = []
    stream = pool.stream_for_testing("mic-a")
    original_reserved = pool._ownerless_reserved_terminal_locked
    original_signal = pool._settlement_adapter.signal_owner
    original_terminal = pool._settlement_adapter.terminal_cas
    original_deactivate = stream._generation.deactivate
    original_abort = handle.abort
    original_stop = handle.stop
    original_close = handle.close
    original_cleanup = store.begin_closing
    original_join = threading.Thread.join

    def wait_for_identity_rollback(timeout=None):
        trace.append("3-identity-rollback-wait")
        allocation_release.set()
        result = original_rollback_wait(timeout)
        if result:
            trace.append("3-identity-rollback")
        return result

    def reserved_terminal(record, *args, **kwargs):
        if record.run_id == "ordered-reserved":
            trace.append("2-reserved-cancel")
        return original_reserved(record, *args, **kwargs)

    def signal_owner(claim):
        if claim.lease_id == "ordered-committed":
            trace.append("4-owner-cancel")
        return original_signal(claim)

    def deactivate():
        trace.append("5-generation-wake")
        return original_deactivate()

    def abort():
        trace.append("6-handle-abort")
        return original_abort()

    def stop():
        trace.append("6-handle-stop")
        return original_stop()

    def close():
        trace.append("6-handle-close")
        return original_close()

    def join(worker, timeout=None):
        if (
            threading.current_thread().name
            == "resident-audio-shutdown-control"
            and worker.name == "resident-ingest-mic-a-1"
        ):
            trace.append("7-ingest-join")
        if (
            threading.current_thread().name == "ordered-shutdown-owner"
            and worker is commit_worker
        ):
            trace.append("7-commit-join")
        return original_join(worker, timeout)

    def terminal(claim, *args, **kwargs):
        if claim.lease_id == "ordered-committed":
            trace.append("8-terminal")
        return original_terminal(claim, *args, **kwargs)

    def cleanup():
        trace.append("9-artifact-cleanup")
        return original_cleanup()

    monkeypatch.setattr(
        pool, "_ownerless_reserved_terminal_locked", reserved_terminal
    )
    monkeypatch.setattr(pool._settlement_adapter, "signal_owner", signal_owner)
    monkeypatch.setattr(pool._settlement_adapter, "terminal_cas", terminal)
    monkeypatch.setattr(stream._generation, "deactivate", deactivate)
    monkeypatch.setattr(handle, "abort", abort)
    monkeypatch.setattr(handle, "stop", stop)
    monkeypatch.setattr(handle, "close", close)
    monkeypatch.setattr(store, "begin_closing", cleanup)
    monkeypatch.setattr(rollback_settled, "wait", wait_for_identity_rollback)
    monkeypatch.setattr(threading.Thread, "join", join)

    reports = []

    def shutdown_owner():
        trace.append("1-reject")
        reports.append(pool.shutdown())
        trace.append("10-report")

    owner = threading.Thread(
        target=shutdown_owner, name="ordered-shutdown-owner"
    )
    owner.start()
    original_join(owner, 2.0)
    original_join(commit_worker, 2.0)

    assert not owner.is_alive()
    assert not commit_worker.is_alive()
    assert commit_outcomes[0].state is LeaseState.CANCELED
    assert reports[0].state is PoolState.CLOSED
    expected = (
        "1-reject",
        "2-reserved-cancel",
        "3-identity-rollback",
        "4-owner-cancel",
        "5-generation-wake",
        "6-handle-abort",
        "6-handle-stop",
        "6-handle-close",
        "7-ingest-join",
        "7-commit-join",
        "8-terminal",
        "9-artifact-cleanup",
        "10-report",
    )
    assert set(expected) <= set(trace), trace
    positions = [trace.index(label) for label in expected]
    assert positions == sorted(positions), trace

    blocked_pool, blocked_resolver, blocked_backend, blocked_store, _ = (
        make_pool(tmp_path / "blocked")
    )
    blocked_handle, blocked_token = make_ready(
        blocked_pool, blocked_resolver, blocked_backend
    )
    blocked_pool.reserve("blocked-close")
    assert blocked_pool.commit(
        "blocked-close",
        blocked_token,
        request(blocked_token, "blocked-close", duration=1.0),
    ).state is LeaseState.COMMITTED
    close_entered = threading.Event()
    close_release = threading.Event()
    post_physical = []
    blocked_original_close = blocked_handle.close
    blocked_original_terminal = (
        blocked_pool._settlement_adapter.terminal_cas
    )

    def blocked_close():
        close_entered.set()
        close_release.wait(1.0)
        return blocked_original_close()

    def observed_terminal(*args, **kwargs):
        post_physical.append("terminal")
        return blocked_original_terminal(*args, **kwargs)

    def observed_cleanup():
        post_physical.append("artifact-cleanup")
        return ArtifactCleanupReport()

    monkeypatch.setattr(blocked_handle, "close", blocked_close)
    monkeypatch.setattr(
        blocked_pool._settlement_adapter, "terminal_cas", observed_terminal
    )
    monkeypatch.setattr(blocked_store, "begin_closing", observed_cleanup)
    blocked_pool._shutdown_control_timeout = 0.0
    blocked_pool._shutdown_force_grace = 0.0
    blocked = blocked_pool.shutdown()
    try:
        assert close_entered.is_set()
        assert blocked.state is PoolState.FAILED_CLOSED
        assert post_physical == []
        assert not blocked_pool.lease_snapshot("blocked-close").settled
    finally:
        close_release.set()
        wait_until(
            lambda: not any(
                resource.resource_kind == "control"
                and resource.thread_alive
                for resource in blocked_pool._collect_shutdown_resources()
            )
        )


def test_shutdown_clears_every_unique_stream_before_first_physical_handle_close(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    ready_ids = ("barrier-a", "barrier-b")
    opening_id = "barrier-opening"
    hardware_ids = ready_ids + (opening_id,)
    trace = []
    first_physical = threading.Event()
    resolver.devices.update(
        {
            hardware_id: device(hardware_id)
            for hardware_id in hardware_ids
        }
    )
    assert pool.reconcile(
        [desired(hardware_id) for hardware_id in ready_ids]
    ).accepted
    streams = {
        hardware_id: pool.stream_for_testing(hardware_id)
        for hardware_id in ready_ids
    }
    for hardware_id in ready_ids:
        handle = backend.handles[hardware_id][0]
        clock.advance(0.1)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        assert pool.synchronize_stream(hardware_id, timeout=1.0)

    def instrument_physical_handle(hardware_id, handle):
        for operation in ("abort", "stop", "close"):
            original_operation = getattr(handle, operation)

            def physical(
                _hardware_id=hardware_id,
                _operation=operation,
                _original=original_operation,
            ):
                trace.append(f"{_operation}-{_hardware_id}")
                first_physical.set()
                return _original()

            monkeypatch.setattr(handle, operation, physical)

    for hardware_id in ready_ids:
        instrument_physical_handle(
            hardware_id, backend.handles[hardware_id][0]
        )
    backend.handle_setups[opening_id] = lambda handle: (
        instrument_physical_handle(opening_id, handle)
    )
    backend.open_releases[opening_id] = threading.Event()
    opening_reconcile = threading.Thread(
        target=lambda: pool.reconcile(
            [desired(hardware_id) for hardware_id in hardware_ids]
        ),
        name="barrier-opening-reconcile",
    )
    opening_reconcile.start()
    assert backend.open_started[opening_id].wait(1.0)
    with pool._lock:
        streams[opening_id] = pool._opening_attempts[opening_id].stream

    duplicate_stream = streams["barrier-a"]
    with pool._lock:
        duplicate_record = pool._streams["barrier-a"]
        pool._closing_streams["closing-duplicate"] = SimpleNamespace(
            action=SimpleNamespace(hardware_id="closing-duplicate"),
            record=duplicate_record,
            settled_event=threading.Event(),
            in_progress=False,
        )
        pool._unpublished_closes[("unpublished-duplicate",)] = (
            SimpleNamespace(
                stream=duplicate_stream,
                settled_event=threading.Event(),
                in_progress=False,
                closed=False,
            )
        )

    original_schedule = pool._schedule_shutdown_close
    schedule_calls = []

    for hardware_id, stream in streams.items():
        original_deactivate = stream._generation.deactivate
        original_wake = stream._wake.set

        def deactivate(
            _hardware_id=hardware_id,
            _original=original_deactivate,
        ):
            trace.append(f"clear-{_hardware_id}")
            return _original()

        def wake(_hardware_id=hardware_id, _original=original_wake):
            trace.append(f"wake-{_hardware_id}")
            return _original()

        monkeypatch.setattr(stream._generation, "deactivate", deactivate)
        monkeypatch.setattr(stream._wake, "set", wake)

    def scheduled(stream, timeout):
        hardware_id = stream.snapshot().hardware_id
        schedule_calls.append(hardware_id)
        backend.open_releases[opening_id].set()
        tracked = original_schedule(stream, timeout)
        if len(schedule_calls) == 1:
            assert first_physical.wait(1.0)
        return tracked

    monkeypatch.setattr(pool, "_schedule_shutdown_close", scheduled)

    result = pool.shutdown()
    opening_reconcile.join(2.0)

    assert result.state is PoolState.CLOSED
    assert not opening_reconcile.is_alive()
    first_physical_index = min(
        index
        for index, event in enumerate(trace)
        if event.startswith(("abort-", "stop-", "close-"))
    )
    for hardware_id in hardware_ids:
        assert trace.index(f"clear-{hardware_id}") < first_physical_index
        assert trace.index(f"wake-{hardware_id}") < first_physical_index
    assert set(ready_ids).issubset(schedule_calls)
    assert set(schedule_calls).issubset(hardware_ids)
    assert len(schedule_calls) == len(set(schedule_calls))
    assert backend.close_order.count("barrier-a") == 1
    assert backend.close_order.count("barrier-b") == 1
    assert backend.close_order.count(opening_id) == 1

    blocked_pool, blocked_resolver, blocked_backend, blocked_store, _ = (
        make_pool(tmp_path / "blocked-clear")
    )
    make_ready(blocked_pool, blocked_resolver, blocked_backend)
    blocked_stream = blocked_pool.stream_for_testing("mic-a")
    clear_entered = threading.Event()
    clear_release = threading.Event()
    post_clear_calls = []
    original_begin = blocked_stream.control_begin_shutdown
    original_blocked_cleanup = blocked_store.begin_closing

    def blocked_begin():
        clear_entered.set()
        assert clear_release.wait(1.0)
        return original_begin()

    monkeypatch.setattr(
        blocked_stream, "control_begin_shutdown", blocked_begin
    )
    monkeypatch.setattr(
        blocked_store,
        "begin_closing",
        lambda: post_clear_calls.append("cleanup"),
    )
    for operation in ("abort", "stop", "close"):
        original_operation = getattr(
            blocked_backend.handles["mic-a"][0], operation
        )

        def observed(
            _operation=operation,
            _original=original_operation,
        ):
            post_clear_calls.append(_operation)
            return _original()

        monkeypatch.setattr(
            blocked_backend.handles["mic-a"][0],
            operation,
            observed,
        )
    blocked_pool._shutdown_control_timeout = 0.0
    blocked_pool._shutdown_force_grace = 0.0
    blocked_reports = []
    blocked_shutdown = threading.Thread(
        target=lambda: blocked_reports.append(blocked_pool.shutdown())
    )
    blocked_shutdown.start()
    assert clear_entered.wait(1.0)
    blocked_shutdown.join(0.2)
    try:
        assert not blocked_shutdown.is_alive()
        assert blocked_reports[0].state is PoolState.FAILED_CLOSED
        assert post_clear_calls == []
    finally:
        clear_release.set()
        blocked_shutdown.join(2.0)
    wait_until(
        lambda: not any(
            resource.resource_kind == "shutdown-begin"
            and resource.thread_alive
            for resource in blocked_pool._collect_shutdown_resources()
        )
    )
    monkeypatch.setattr(
        blocked_store, "begin_closing", original_blocked_cleanup
    )
    blocked_pool._shutdown_control_timeout = 1.0
    recovered = blocked_pool.shutdown()
    assert recovered.state is PoolState.CLOSED
    assert post_clear_calls == ["abort", "stop", "close"]
    assert blocked_backend.force_terminate_calls == 1


def test_shutdown_cancellation_gap_reserves_late_open_physical_close(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    ready_id = "gap-ready"
    opening_id = "gap-opening"
    resolver.devices.update(
        {
            ready_id: device(ready_id),
            opening_id: device(opening_id),
        }
    )
    assert pool.reconcile([desired(ready_id)]).accepted
    ready_stream = pool.stream_for_testing(ready_id)
    backend.open_releases[opening_id] = threading.Event()
    opening_reconcile = threading.Thread(
        target=lambda: pool.reconcile(
            [desired(ready_id), desired(opening_id)]
        ),
        name="gap-opening-reconcile",
    )
    opening_reconcile.start()
    assert backend.open_started[opening_id].wait(1.0)
    with pool._lock:
        opening_attempt = pool._opening_attempts[opening_id]
        opening_stream = opening_attempt.stream

    gap_entered = threading.Event()
    gap_release = threading.Event()
    post_barrier_work = []
    original_barrier = pool._run_stream_shutdown_barrier
    original_cleanup = store.begin_closing
    original_terminal = pool._settlement_adapter.terminal_cas

    def blocked_barrier(streams, deadline):
        gap_entered.set()
        assert gap_release.wait(2.0)
        return original_barrier(streams, deadline)

    def observed_cleanup():
        post_barrier_work.append("artifact-cleanup")
        return original_cleanup()

    def observed_terminal(*args, **kwargs):
        post_barrier_work.append("terminal")
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(
        pool, "_run_stream_shutdown_barrier", blocked_barrier
    )
    monkeypatch.setattr(store, "begin_closing", observed_cleanup)
    monkeypatch.setattr(
        pool._settlement_adapter, "terminal_cas", observed_terminal
    )
    reports = []
    shutdown_thread = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="gap-shutdown",
    )
    shutdown_thread.start()
    assert gap_entered.wait(1.0)
    assert opening_attempt.cancel_token.is_set()
    assert ready_stream.generation_latch.alive

    backend.open_releases[opening_id].set()
    assert opening_stream._startup._settled.wait(1.0)
    opening_reconcile.join(1.0)
    late_handle = backend.handles[opening_id][0]
    try:
        assert not opening_reconcile.is_alive()
        assert ready_stream.generation_latch.alive
        assert [
            call
            for call in late_handle.calls
            if call in ("abort", "stop", "close")
        ] == []
        assert backend.close_order.count(opening_id) == 0
        assert post_barrier_work == []
    finally:
        gap_release.set()

    shutdown_thread.join(2.0)
    assert not shutdown_thread.is_alive()
    assert reports[0].state is PoolState.CLOSED
    assert late_handle.calls[-3:] == ["abort", "stop", "close"]
    assert all(
        late_handle.calls.count(operation) == 1
        for operation in ("abort", "stop", "close")
    )
    assert backend.close_order.count(opening_id) == 1
    assert backend.close_threads[-1] == "resident-audio-shutdown-control"


def test_pretransition_close_permission_drains_before_shutdown_and_barrier(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    ready_id = "lease-ready"
    closing_id = "lease-closing"
    hardware_ids = (ready_id, closing_id)
    resolver.devices.update(
        {
            hardware_id: device(hardware_id)
            for hardware_id in hardware_ids
        }
    )
    assert pool.reconcile(
        [desired(hardware_id) for hardware_id in hardware_ids]
    ).accepted
    for hardware_id in hardware_ids:
        handle = backend.handles[hardware_id][0]
        clock.advance(0.1)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        assert pool.synchronize_stream(hardware_id, timeout=1.0)

    ready_stream = pool.stream_for_testing(ready_id)
    closing_stream = pool.stream_for_testing(closing_id)
    closing_handle = backend.handles[closing_id][0]
    permission_decided = threading.Event()
    permission_release = threading.Event()
    close_returned = threading.Event()
    publication_release = threading.Event()
    reservation_entered = threading.Event()
    barrier_entered = threading.Event()
    barrier_release = threading.Event()
    barrier_complete = threading.Event()
    operation_states = []
    permit_calls = 0
    first_close = True
    original_permits = pool._physical_close_gate.permits
    original_begin_shutdown = pool._physical_close_gate.begin_shutdown
    original_close = closing_stream.close
    original_ready_begin = ready_stream.control_begin_shutdown

    def paused_permission(stream, *, control_path, **kwargs):
        nonlocal permit_calls
        permitted = original_permits(
            stream, control_path=control_path, **kwargs
        )
        if stream is closing_stream and permitted:
            permit_calls += 1
            if kwargs.get("begin_physical") or permit_calls == 2:
                permission_decided.set()
                assert permission_release.wait(2.0)
        return permitted

    def close_before_publication(*args, **kwargs):
        nonlocal first_close
        result = original_close(*args, **kwargs)
        if first_close:
            first_close = False
            close_returned.set()
            assert publication_release.wait(2.0)
        return result

    def observed_reservation():
        result = original_begin_shutdown()
        reservation_entered.set()
        return result

    def blocked_ready_begin(*args, **kwargs):
        barrier_entered.set()
        assert barrier_release.wait(2.0)
        result = original_ready_begin(*args, **kwargs)
        barrier_complete.set()
        return result

    monkeypatch.setattr(
        pool._physical_close_gate, "permits", paused_permission
    )
    monkeypatch.setattr(
        pool._physical_close_gate,
        "begin_shutdown",
        observed_reservation,
    )
    monkeypatch.setattr(closing_stream, "close", close_before_publication)
    monkeypatch.setattr(
        ready_stream, "control_begin_shutdown", blocked_ready_begin
    )
    for operation in ("abort", "stop", "close"):
        original_operation = getattr(closing_handle, operation)

        def observed_operation(
            _operation=operation,
            _original=original_operation,
        ):
            operation_states.append(
                (_operation, pool.snapshot().state)
            )
            return _original()

        monkeypatch.setattr(
            closing_handle, operation, observed_operation
        )

    reconcile_thread = threading.Thread(
        target=lambda: pool.reconcile([desired(ready_id)]),
        name="pretransition-close-reconcile",
    )
    reconcile_thread.start()
    assert permission_decided.wait(1.0)
    reports = []
    shutdown_thread = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="pretransition-close-shutdown",
    )
    shutdown_thread.start()
    assert reservation_entered.wait(1.0)
    try:
        with pool._lock:
            assert pool._state is PoolState.OPEN
        assert not barrier_entered.is_set()
        permission_release.set()
        assert close_returned.wait(1.0)
        assert all(
            state is PoolState.OPEN
            for _operation, state in operation_states
        )
        with pool._lock:
            assert pool._state is PoolState.OPEN
        assert not barrier_entered.is_set()
        publication_release.set()
        reconcile_thread.join(2.0)
        assert barrier_entered.wait(1.0)
        assert not barrier_complete.is_set()
        assert all(
            state is PoolState.OPEN
            for _operation, state in operation_states
        )
    finally:
        permission_release.set()
        publication_release.set()
        barrier_release.set()
        reconcile_thread.join(2.0)
        shutdown_thread.join(2.0)

    assert not reconcile_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert reports[0].state is PoolState.CLOSED
    assert [operation for operation, _state in operation_states] == [
        "abort",
        "stop",
        "close",
    ]
    assert backend.close_order.count(closing_id) == 1


def test_shutdown_close_lease_deadline_fails_closed_and_retries_once(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    ready_id = "lease-deadline-ready"
    closing_id = "lease-deadline-closing"
    resolver.devices.update(
        {
            hardware_id: device(hardware_id)
            for hardware_id in (ready_id, closing_id)
        }
    )
    assert pool.reconcile(
        [desired(ready_id), desired(closing_id)]
    ).accepted
    for hardware_id in (ready_id, closing_id):
        handle = backend.handles[hardware_id][0]
        clock.advance(0.1)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        assert pool.synchronize_stream(hardware_id, timeout=1.0)

    closing_stream = pool.stream_for_testing(closing_id)
    closing_handle = backend.handles[closing_id][0]
    lease_acquired = threading.Event()
    lease_release = threading.Event()
    original_acquire = pool._physical_close_gate.acquire
    pause_once = True

    def paused_acquire(stream, *, control_path):
        nonlocal pause_once
        lease = original_acquire(stream, control_path=control_path)
        if stream is closing_stream and pause_once:
            pause_once = False
            lease_acquired.set()
            assert lease_release.wait(2.0)
        return lease

    monkeypatch.setattr(
        pool._physical_close_gate, "acquire", paused_acquire
    )
    reconcile_thread = threading.Thread(
        target=lambda: pool.reconcile([desired(ready_id)]),
        name="close-lease-deadline-reconcile",
    )
    reconcile_thread.start()
    assert lease_acquired.wait(1.0)
    pool._shutdown_control_timeout = 0.0

    first = pool.shutdown()

    assert first.state is PoolState.FAILED_CLOSED
    assert first.shutdown_error_code is CaptureErrorCode.SHUTDOWN_INCOMPLETE
    assert pool._shutdown_incomplete_resources[
        "physical_close_leases"
    ] == 1
    assert backend.force_terminate_calls == 0
    assert [
        call
        for call in closing_handle.calls
        if call in ("abort", "stop", "close")
    ] == []

    lease_release.set()
    reconcile_thread.join(2.0)
    assert not reconcile_thread.is_alive()
    assert pool._physical_close_gate.active_lease_count == 0
    assert closing_handle.calls[-3:] == ["abort", "stop", "close"]
    assert all(
        closing_handle.calls.count(operation) == 1
        for operation in ("abort", "stop", "close")
    )
    pool._shutdown_control_timeout = 1.0

    retried = pool.shutdown()

    assert retried.state is PoolState.CLOSED
    assert closing_handle.calls[-3:] == ["abort", "stop", "close"]
    assert all(
        closing_handle.calls.count(operation) == 1
        for operation in ("abort", "stop", "close")
    )
    assert backend.close_order.count(closing_id) == 1


def test_late_open_handle_waits_for_global_shutdown_step_six_authorization(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    ready_id = "barrier-ready"
    opening_id = "barrier-late-open"
    resolver.devices.update(
        {
            ready_id: device(ready_id),
            opening_id: device(opening_id),
        }
    )
    assert pool.reconcile([desired(ready_id)]).accepted
    ready_stream = pool.stream_for_testing(ready_id)
    backend.open_releases[opening_id] = threading.Event()
    opening_reconcile = threading.Thread(
        target=lambda: pool.reconcile(
            [desired(ready_id), desired(opening_id)]
        ),
        name="late-open-reconcile",
    )
    opening_reconcile.start()
    assert backend.open_started[opening_id].wait(1.0)
    with pool._lock:
        opening_stream = pool._opening_attempts[opening_id].stream

    ready_begin_entered = threading.Event()
    ready_begin_release = threading.Event()
    opening_begin_complete = threading.Event()
    original_ready_begin = ready_stream.control_begin_shutdown
    original_opening_begin = opening_stream.control_begin_shutdown
    post_handle_work = []
    original_cleanup = store.begin_closing
    original_terminal = pool._settlement_adapter.terminal_cas

    def blocked_ready_begin(*args, **kwargs):
        ready_begin_entered.set()
        assert ready_begin_release.wait(2.0)
        return original_ready_begin(*args, **kwargs)

    def observed_opening_begin(*args, **kwargs):
        result = original_opening_begin(*args, **kwargs)
        opening_begin_complete.set()
        return result

    def observed_cleanup():
        post_handle_work.append("artifact-cleanup")
        return original_cleanup()

    def observed_terminal(*args, **kwargs):
        post_handle_work.append("terminal")
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(
        ready_stream, "control_begin_shutdown", blocked_ready_begin
    )
    monkeypatch.setattr(
        opening_stream,
        "control_begin_shutdown",
        observed_opening_begin,
    )
    monkeypatch.setattr(store, "begin_closing", observed_cleanup)
    monkeypatch.setattr(
        pool._settlement_adapter, "terminal_cas", observed_terminal
    )

    reports = []
    shutdown_thread = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="late-open-shutdown",
    )
    shutdown_thread.start()
    assert ready_begin_entered.wait(1.0)
    assert opening_begin_complete.wait(1.0)
    assert not opening_stream._generation.alive

    backend.open_releases[opening_id].set()
    wait_until(lambda: bool(backend.handles[opening_id]))
    late_handle = backend.handles[opening_id][0]
    assert opening_stream._startup._settled.wait(1.0)
    try:
        assert late_handle.calls == []
        assert backend.close_order.count(opening_id) == 0
        assert opening_stream.tracked_cleanup_handle is late_handle
        assert post_handle_work == []
    finally:
        ready_begin_release.set()

    shutdown_thread.join(2.0)
    opening_reconcile.join(2.0)
    assert not shutdown_thread.is_alive()
    assert not opening_reconcile.is_alive()
    assert reports[0].state is PoolState.CLOSED
    assert late_handle.calls == ["abort", "stop", "close"]
    assert backend.close_order.count(opening_id) == 1
    assert backend.close_threads[-1] == "resident-audio-shutdown-control"


def test_retry_settlements_waits_for_registered_terminal_execution_visibility(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    pool.set_completion_handler(
        lambda completion: completion.claim("visibility-consumer")
    )
    handle, token = make_ready(pool, resolver, backend)
    terminal_entered = threading.Event()
    terminal_release = threading.Event()
    original_execute = pool._execute_owner_terminal

    def blocked_terminal(owner, event):
        terminal_entered.set()
        assert terminal_release.wait(2.0)
        return original_execute(owner, event)

    monkeypatch.setattr(pool, "_execute_owner_terminal", blocked_terminal)
    pool.reserve("terminal-visibility")
    assert pool.commit(
        "terminal-visibility",
        token,
        request(token, "terminal-visibility", duration=0.1),
    ).state is LeaseState.COMMITTED
    owner = pool._leases["terminal-visibility"].owner
    clock.advance(0.05)
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert pool.synchronize_stream("mic-a", timeout=1.0)
    assert terminal_entered.wait(1.0)
    assert owner.join(2.0)
    with pool._lock:
        record = pool._leases["terminal-visibility"]
        assert record.settlement_claim is None
        assert len(pool._terminal_executions) == 1

    retry_started = threading.Event()
    retry_returned = threading.Event()
    retry_results = []
    retry_errors = []

    def retry():
        retry_started.set()
        try:
            retry_results.append(pool.retry_settlements())
        except Exception as error:
            retry_errors.append(error)
        finally:
            retry_returned.set()

    retry_thread = threading.Thread(
        target=retry, name="terminal-visibility-retry"
    )
    retry_thread.start()
    assert retry_started.wait(1.0)
    try:
        assert not retry_returned.wait(0.1)
        assert pool._leases["terminal-visibility"].state is LeaseState.COMMITTED
    finally:
        terminal_release.set()

    retry_thread.join(2.0)
    assert not retry_thread.is_alive()
    assert retry_errors == []
    assert retry_results[0].unsettled_transactions == 0
    assert (
        pool._leases["terminal-visibility"].state is LeaseState.RELEASED
    )
    assert pool.lease_snapshot("terminal-visibility").settled
    pool.shutdown()


def test_force_worker_live_and_exceptional_outcomes_are_positive_shutdown_resources(
    tmp_path, monkeypatch
):
    live_pool, live_resolver, live_backend, _, _ = make_pool(
        tmp_path / "live"
    )
    make_ready(live_pool, live_resolver, live_backend)
    live_stream = live_pool.stream_for_testing("mic-a")
    live_close_release = threading.Event()
    force_entered = threading.Event()
    force_return_release = threading.Event()
    live_original_close = live_stream.close

    def live_close_after_force(*args, **kwargs):
        assert live_close_release.wait(1.0)
        return live_original_close(*args, **kwargs)

    def live_force():
        live_backend.force_terminate_calls += 1
        live_close_release.set()
        force_entered.set()
        assert force_return_release.wait(2.0)

    monkeypatch.setattr(live_stream, "close", live_close_after_force)
    monkeypatch.setattr(live_backend, "force_terminate", live_force)
    live_pool._shutdown_control_timeout = 0.0
    live_pool._shutdown_force_grace = 0.0
    live_first = live_pool.shutdown()
    assert force_entered.is_set()
    assert live_first.state is PoolState.FAILED_CLOSED
    assert any(
        resource.resource_kind == "backend-force-terminate"
        and resource.thread_alive
        for resource in live_first.shutdown_resources
    )
    live_close_release.set()
    live_pool._shutdown_control_timeout = 0.2
    for _attempt in range(3):
        live_while_force_running = live_pool.shutdown()
        assert live_while_force_running.state is PoolState.FAILED_CLOSED
        kinds = {
            resource.resource_kind
            for resource in live_while_force_running.shutdown_resources
        }
        if kinds == {"backend-force-terminate"}:
            break
    assert kinds == {"backend-force-terminate"}
    assert any(
        resource.thread_alive
        for resource in live_while_force_running.shutdown_resources
    )
    force_return_release.set()
    wait_until(lambda: not live_pool._force_terminate_work.worker.is_alive())
    live_after_force_return = live_pool.shutdown()
    assert live_after_force_return.state is PoolState.CLOSED
    assert live_after_force_return.shutdown_resources == ()
    assert live_backend.force_terminate_calls == 1

    pool, resolver, backend, _, _ = make_pool(tmp_path / "exceptional")
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    release_close = threading.Event()
    original_close = stream.close

    class ForceFailure(RuntimeError):
        pass

    def close_after_force(*args, **kwargs):
        assert release_close.wait(1.0)
        return original_close(*args, **kwargs)

    def exceptional_force():
        backend.force_terminate_calls += 1
        release_close.set()
        raise ForceFailure("sensitive backend payload")

    monkeypatch.setattr(stream, "close", close_after_force)
    monkeypatch.setattr(backend, "force_terminate", exceptional_force)
    pool._shutdown_control_timeout = 0.0
    pool._shutdown_force_grace = 0.2

    first = pool.shutdown()

    assert first.state is PoolState.FAILED_CLOSED
    force_resource = next(
        resource
        for resource in first.shutdown_resources
        if resource.resource_kind == "backend-force-terminate"
    )
    assert not force_resource.thread_alive
    assert force_resource.exception_type == "ForceFailure"
    assert "sensitive backend payload" not in repr(first)
    second = pool.shutdown()
    assert second.state is PoolState.FAILED_CLOSED
    assert backend.force_terminate_calls == 1


def test_shutdown_requires_typed_artifact_cleanup_proof_and_retries_malformed_results(
    tmp_path, monkeypatch
):
    pool, _, _, store, _ = make_pool(tmp_path)
    cleanup_calls = []

    class MalformedCleanup:
        def __repr__(self):
            return "<sensitive-cleanup-result>"

    results = [None, MalformedCleanup(), ArtifactCleanupReport()]

    def cleanup():
        cleanup_calls.append("begin-closing")
        return results.pop(0)

    monkeypatch.setattr(store, "begin_closing", cleanup)

    missing = pool.shutdown()
    assert missing.state is PoolState.FAILED_CLOSED
    assert missing.cleanup_exception_type == "NoneType"
    assert "sensitive-cleanup-result" not in repr(missing)

    malformed = pool.shutdown()
    assert malformed.state is PoolState.FAILED_CLOSED
    assert malformed.cleanup_exception_type == "MalformedCleanup"
    assert "sensitive-cleanup-result" not in repr(malformed)

    complete = pool.shutdown()
    assert complete.state is PoolState.CLOSED
    assert complete.cleanup_exception_type is None
    assert cleanup_calls == ["begin-closing"] * 3


def test_shutdown_force_terminates_backend_once_then_closes_with_one_grace(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    original_close = stream.close
    close_entered = threading.Event()
    force_released = threading.Event()

    def blocked_close(*args, **kwargs):
        close_entered.set()
        assert force_released.wait(1.0)
        return original_close(*args, **kwargs)

    def force_terminate():
        backend.force_terminate_calls += 1
        force_released.set()

    monkeypatch.setattr(stream, "close", blocked_close)
    monkeypatch.setattr(backend, "force_terminate", force_terminate)
    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.2

    result = pool.shutdown()

    assert close_entered.is_set()
    assert backend.force_terminate_calls == 1
    assert result.state is PoolState.CLOSED
    assert result.shutdown_error_code is None
    assert result.shutdown_resources == ()
    assert pool.shutdown() is result
    assert backend.force_terminate_calls == 1


def test_shutdown_incomplete_reports_exact_live_handle_and_ingest_resource(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    close_entered = threading.Event()
    release_close = threading.Event()

    def blocked_close(*_args, **_kwargs):
        close_entered.set()
        release_close.wait(1.0)
        return False

    monkeypatch.setattr(stream, "close", blocked_close)
    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.02

    result = pool.shutdown()
    try:
        assert close_entered.is_set()
        assert backend.force_terminate_calls == 1
        assert result.state is PoolState.FAILED_CLOSED
        assert result.shutdown_error_code is CaptureErrorCode.SHUTDOWN_INCOMPLETE
        assert {
            resource.resource_kind
            for resource in result.shutdown_resources
        } == {"stream", "control"}
        resource = next(
            item
            for item in result.shutdown_resources
            if item.resource_kind == "stream"
        )
        assert resource.hardware_id == "mic-a"
        assert resource.stream_generation == 1
        assert resource.thread_name == "resident-ingest-mic-a-1"
        assert not resource.thread_alive
        assert resource.handle_open
    finally:
        release_close.set()


def test_force_termination_waits_for_the_single_cooperative_deadline(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    monkeypatch.setattr(stream, "close", lambda **_kwargs: False)
    forced_at = []
    started = time.monotonic()

    def force_terminate():
        backend.force_terminate_calls += 1
        forced_at.append(time.monotonic() - started)

    monkeypatch.setattr(backend, "force_terminate", force_terminate)
    pool._shutdown_control_timeout = 0.05
    pool._shutdown_force_grace = 0.01

    result = pool.shutdown()

    assert result.state is PoolState.FAILED_CLOSED
    assert backend.force_terminate_calls == 1
    assert forced_at[0] >= 0.04


def test_shutdown_closes_stream_before_owner_join_artifact_and_terminal(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("ordered-shutdown")
    assert pool.commit(
        "ordered-shutdown",
        token,
        request(token, "ordered-shutdown", duration=1.0),
    ).state is LeaseState.COMMITTED
    stream = pool.stream_for_testing("mic-a")
    adapter = pool._settlement_adapter
    order = []

    def observed(target, attribute, label):
        original = getattr(target, attribute)

        def wrapped(*args, **kwargs):
            order.append(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(target, attribute, wrapped)

    observed(stream, "close", "stream-close")
    observed(adapter, "join_owner", "owner-join")
    observed(
        adapter,
        "settle_artifact_and_publication",
        "artifact-publication",
    )
    observed(adapter, "terminal_cas", "terminal-cas")

    result = pool.shutdown()

    assert result.state is PoolState.CLOSED
    assert order.index("stream-close") < order.index("owner-join")
    assert order.index("owner-join") < order.index("artifact-publication")
    assert order.index("artifact-publication") < order.index("terminal-cas")


def test_blocked_stream_teardown_keeps_settlement_before_post_close_stages(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("teardown-gate")
    pool.commit(
        "teardown-gate",
        token,
        request(token, "teardown-gate", duration=1.0),
    )
    stream = pool.stream_for_testing("mic-a")
    close_entered = threading.Event()
    release_close = threading.Event()
    post_teardown_calls = []
    adapter = pool._settlement_adapter
    original_close = stream.close

    def blocked_close(*args, **kwargs):
        close_entered.set()
        if not release_close.wait(1.0):
            return False
        return original_close(*args, **kwargs)

    for name in (
        "join_owner",
        "check_artifact_readiness",
        "acknowledge_finality",
        "prune_finality",
        "settle_artifact_and_publication",
        "terminal_cas",
    ):
        original = getattr(adapter, name)

        def observed(*args, _name=name, _original=original, **kwargs):
            post_teardown_calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(adapter, name, observed)
    monkeypatch.setattr(stream, "close", blocked_close)
    pool._shutdown_control_timeout = 0.03
    pool._shutdown_force_grace = 0.03

    first = pool.shutdown()
    try:
        assert close_entered.is_set()
        assert first.state is PoolState.FAILED_CLOSED
        assert post_teardown_calls == []
        assert not pool.lease_snapshot("teardown-gate").settled
        claim = pool._leases["teardown-gate"].settlement_claim
        assert claim is not None
        assert claim.progress.owner_signaled
        assert not claim.progress.owner_joined
    finally:
        release_close.set()
    wait_until(
        lambda: not any(
            resource.thread_alive
            for resource in pool._collect_shutdown_resources()
            if resource.resource_kind == "control"
        )
    )
    assert pool.shutdown().state is PoolState.CLOSED
    assert pool.lease_snapshot("teardown-gate").settled


def test_cleanup_set_event_before_worker_return_cannot_publish_closed(
    tmp_path, monkeypatch
):
    pool, _, _, _, _ = make_pool(tmp_path)
    event_set_before_return = threading.Event()
    release_return = threading.Event()
    original_set = threading.Event.set

    def set_then_hold(event):
        original_set(event)
        work = pool._artifact_cleanup_work
        if (
            work is not None
            and event is work.settled
            and threading.current_thread() is work.worker
        ):
            original_set(event_set_before_return)
            release_return.wait(1.0)

    monkeypatch.setattr(threading.Event, "set", set_then_hold)
    pool._shutdown_control_timeout = 0.03
    reports = []
    shutdown = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="cleanup-event-race-shutdown",
    )
    shutdown.start()
    assert event_set_before_return.wait(1.0)
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert reports[0].state is PoolState.FAILED_CLOSED
        resource = next(
            item
            for item in reports[0].shutdown_resources
            if item.resource_kind == "artifact-cleanup"
        )
        assert resource.thread_name == "resident-audio-artifact-cleanup"
        assert resource.thread_alive
    finally:
        release_return.set()
        shutdown.join(2.0)


def test_force_set_event_before_worker_return_cannot_publish_closed(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    release_close = threading.Event()
    force_event_set_before_return = threading.Event()
    release_force_return = threading.Event()
    original_close = stream.close
    original_set = threading.Event.set

    def close_after_force(*args, **kwargs):
        if not release_close.wait(1.0):
            return False
        return original_close(*args, **kwargs)

    def force_terminate():
        backend.force_terminate_calls += 1
        release_close.set()

    def set_then_hold(event):
        original_set(event)
        work = pool._force_terminate_work
        if (
            work is not None
            and event is work.settled
            and threading.current_thread() is work.worker
        ):
            original_set(force_event_set_before_return)
            release_force_return.wait(1.0)

    monkeypatch.setattr(stream, "close", close_after_force)
    monkeypatch.setattr(backend, "force_terminate", force_terminate)
    monkeypatch.setattr(threading.Event, "set", set_then_hold)
    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.05
    reports = []
    shutdown = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="force-event-race-shutdown",
    )
    shutdown.start()
    assert force_event_set_before_return.wait(1.0)
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert reports[0].state is PoolState.FAILED_CLOSED
        resource = next(
            item
            for item in reports[0].shutdown_resources
            if item.resource_kind == "backend-force-terminate"
        )
        assert resource.thread_name == (
            "resident-audio-backend-force-terminate"
        )
        assert resource.thread_alive
    finally:
        release_force_return.set()
        shutdown.join(2.0)


def test_blocked_artifact_cleanup_is_bounded_and_reported_as_live_work(
    tmp_path, monkeypatch
):
    pool, _, _, store, _ = make_pool(tmp_path)
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    reports = []

    def blocked_cleanup():
        cleanup_entered.set()
        cleanup_release.wait(1.0)
        return ArtifactCleanupReport()

    monkeypatch.setattr(store, "begin_closing", blocked_cleanup)
    pool._shutdown_control_timeout = 0.05
    shutdown = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    shutdown.start()
    assert cleanup_entered.wait(1.0)
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert reports[0].state is PoolState.FAILED_CLOSED
        assert reports[0].shutdown_error_code is CaptureErrorCode.SHUTDOWN_INCOMPLETE
        assert any(
            resource.resource_kind == "artifact-cleanup"
            and resource.thread_alive
            for resource in reports[0].shutdown_resources
        )
    finally:
        cleanup_release.set()
        shutdown.join(2.0)


def test_late_successful_artifact_cleanup_is_harvested_without_restarting(
    tmp_path, monkeypatch
):
    pool, _, _, store, _ = make_pool(tmp_path)
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    cleanup_calls = []

    def late_cleanup():
        cleanup_calls.append("begin-closing")
        cleanup_entered.set()
        cleanup_release.wait(1.0)
        return ArtifactCleanupReport(
            deleted_paths=("C:/staging/late.partial",)
        )

    monkeypatch.setattr(store, "begin_closing", late_cleanup)
    pool._shutdown_control_timeout = 0.03

    first = pool.shutdown()
    assert cleanup_entered.is_set()
    assert first.state is PoolState.FAILED_CLOSED
    cleanup_release.set()
    wait_until(
        lambda: (
            pool._artifact_cleanup_work.settled.is_set()
            and not pool._artifact_cleanup_work.worker.is_alive()
        )
    )

    second = pool.shutdown()

    assert second.state is PoolState.CLOSED
    assert second.cleanup_deleted_paths == (
        "C:/staging/late.partial",
    )
    assert cleanup_calls == ["begin-closing"]


def test_exceptional_artifact_cleanup_retains_one_management_retry(
    tmp_path, monkeypatch
):
    pool, _, _, store, _ = make_pool(tmp_path)
    pool.reserve("cleanup-exception")
    cleanup_calls = []
    terminal_calls = []
    original_terminal = pool._ownerless_reserved_terminal_locked

    class CleanupFailure(RuntimeError):
        pass

    def cleanup():
        cleanup_calls.append("begin-closing")
        if len(cleanup_calls) == 1:
            raise CleanupFailure("sensitive cleanup payload")
        return ArtifactCleanupReport()

    def terminal(*args, **kwargs):
        terminal_calls.append(args[0].run_id)
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(store, "begin_closing", cleanup)
    monkeypatch.setattr(
        pool, "_ownerless_reserved_terminal_locked", terminal
    )

    first = pool.shutdown()
    assert first.state is PoolState.FAILED_CLOSED
    assert first.cleanup_exception_type == "CleanupFailure"
    assert "sensitive cleanup payload" not in repr(first)

    second = pool.shutdown()

    assert second.state is PoolState.CLOSED
    assert second.cleanup_exception_type is None
    assert cleanup_calls == ["begin-closing", "begin-closing"]
    assert terminal_calls == ["cleanup-exception"]


def test_blocked_force_termination_is_bounded_by_one_grace_and_reported(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    monkeypatch.setattr(stream, "close", lambda **_kwargs: False)
    force_entered = threading.Event()
    force_release = threading.Event()
    reports = []

    def blocked_force():
        backend.force_terminate_calls += 1
        force_entered.set()
        force_release.wait(1.0)

    monkeypatch.setattr(backend, "force_terminate", blocked_force)
    pool._shutdown_control_timeout = 0.03
    pool._shutdown_force_grace = 0.03
    shutdown = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    shutdown.start()
    assert force_entered.wait(1.0)
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert backend.force_terminate_calls == 1
        assert reports[0].state is PoolState.FAILED_CLOSED
        assert any(
            resource.resource_kind == "backend-force-terminate"
            and resource.thread_alive
            for resource in reports[0].shutdown_resources
        )
    finally:
        force_release.set()
        shutdown.join(2.0)


def test_cleanup_report_paths_block_closed_and_management_retries_exact_target(
    tmp_path, monkeypatch
):
    pool, _, _, store, _ = make_pool(tmp_path)
    pool.reserve("cleanup-target")
    pool._leases["cleanup-target"].artifact_id = "artifact-target"
    reports = [
        ArtifactCleanupReport(
            failed_paths=("C:/staging/failed.partial",),
            retained_paths=("C:/staging/claimed.ready",),
        ),
        ArtifactCleanupReport(),
    ]
    exact_retries = []
    delete_pending = {"value": True}

    monkeypatch.setattr(store, "begin_closing", lambda: reports.pop(0))
    monkeypatch.setattr(
        store,
        "state",
        lambda artifact_id: (
            ArtifactState.DELETE_PENDING
            if artifact_id == "artifact-target" and delete_pending["value"]
            else ArtifactState.DELETED
            if artifact_id == "artifact-target"
            else None
        ),
    )

    def retry_exact(artifact_id):
        exact_retries.append(artifact_id)
        delete_pending["value"] = False
        return ArtifactCleanupReport(
            deleted_paths=("C:/staging/failed.partial",),
            attempted_paths=("C:/staging/failed.partial",),
        )

    monkeypatch.setattr(
        store, "retry_delete_pending_artifact", retry_exact
    )

    first = pool.shutdown()
    assert first.state is PoolState.FAILED_CLOSED
    assert first.cleanup_failed_paths == ("C:/staging/failed.partial",)
    assert first.cleanup_retained_paths == ("C:/staging/claimed.ready",)

    second = pool.shutdown()
    assert exact_retries == ["artifact-target"]
    assert second.state is PoolState.CLOSED
    assert second.cleanup_deleted_paths == ("C:/staging/failed.partial",)


def test_blocked_post_teardown_settlement_is_bounded_and_reported_terminal_work(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("blocked-terminal")
    pool.commit(
        "blocked-terminal",
        token,
        request(token, "blocked-terminal", duration=1.0),
    )
    adapter = pool._settlement_adapter
    original = adapter.settle_artifact_and_publication
    entered = threading.Event()
    release = threading.Event()
    reports = []

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(1.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "settle_artifact_and_publication", blocked)
    pool._shutdown_control_timeout = 0.05
    shutdown = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    shutdown.start()
    assert entered.wait(1.0)
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert reports[0].state is PoolState.FAILED_CLOSED
        assert any(
            resource.resource_kind == "terminal"
            and resource.thread_name
            == "resident-audio-stream-failure-settlement"
            and resource.run_id == "blocked-terminal"
            and resource.hardware_id == "mic-a"
            and resource.stream_generation == 1
            and resource.thread_alive
            for resource in reports[0].shutdown_resources
        )
    finally:
        release.set()
        shutdown.join(2.0)


def test_blocked_pre_teardown_removal_is_bounded_by_cooperative_deadline(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("blocked-removal")
    pool.commit(
        "blocked-removal",
        token,
        request(token, "blocked-removal", duration=1.0),
    )
    adapter = pool._settlement_adapter
    original = adapter.remove_owner
    entered = threading.Event()
    release = threading.Event()
    reports = []

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(1.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "remove_owner", blocked)
    pool._shutdown_control_timeout = 0.05
    pool._shutdown_force_grace = 0.05
    shutdown = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    shutdown.start()
    assert entered.wait(1.0)
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert reports[0].state is PoolState.FAILED_CLOSED
        assert any(
            resource.resource_kind == "terminal"
            and resource.thread_alive
            for resource in reports[0].shutdown_resources
        ), reports[0].shutdown_resources
    finally:
        release.set()
        shutdown.join(2.0)


def test_lease_diagnostics_expose_applicable_artifact_state_and_path(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    assert pool.reserve("diagnostic-run").state is LeaseState.RESERVED
    assert pool.commit(
        "diagnostic-run",
        token,
        request(token, "diagnostic-run", duration=1.0),
    ).state is LeaseState.COMMITTED

    snapshot = pool.lease_snapshot("diagnostic-run")
    assert snapshot.artifact_state is ArtifactState.PARTIAL
    assert snapshot.artifact_path is not None
    assert snapshot.artifact_path.endswith(".partial")
    assert "capability" not in repr(snapshot)
    assert pool.snapshot().committed_leases == 1

    assert pool.cancel(
        "diagnostic-run", "diagnostic cleanup"
    ).state is LeaseState.CANCELED
    assert (
        pool.lease_snapshot("diagnostic-run").artifact_state
        is ArtifactState.DELETED
    )
    pool.shutdown()


def test_structured_diagnostics_cover_transitions_owner_and_settlement(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("structured")
    pool.commit(
        "structured",
        token,
        request(token, "structured", duration=1.0),
    )

    stream_snapshot = pool.snapshot().stream_for("mic-a")
    lease = pool.lease_snapshot("structured")
    assert stream_snapshot.previous_state is StreamState.OPENING
    assert lease.previous_state is LeaseState.COMMITTING
    assert lease.hardware_id == "mic-a"
    assert lease.stream_generation == token.stream_generation
    assert lease.owner_snapshot is not None
    assert (
        lease.owner_snapshot.previous_state,
        lease.owner_snapshot.state,
    ) in (
        (
            CaptureOwnerState.PREPARED,
            CaptureOwnerState.CATCHING_UP,
        ),
        (
            CaptureOwnerState.CATCHING_UP,
            CaptureOwnerState.LIVE,
        ),
    )
    assert lease.owner_snapshot.original_trigger_monotonic == 9.99
    assert lease.owner_snapshot.written_frames >= 0
    assert lease.owner_snapshot.credits_occupied <= 8
    assert lease.owner_snapshot.drain_thread_name
    assert lease.owner_snapshot.drain_thread_alive
    assert lease.artifact_consumer_id is None
    assert lease.error_code is None

    owner = pool._leases["structured"].owner
    monkeypatch.setattr(owner, "join", lambda _timeout=None: False)
    pool.cancel("structured", "diagnostic retry")
    pool.cancel("structured", "diagnostic retry")

    settlement = pool.snapshot().settlement_diagnostics
    assert settlement is not None
    assert dict(settlement.claims_by_phase)
    assert settlement.unsettled_leases == 1
    assert settlement.owner_join_retries >= 1
    assert settlement.claims[0].run_id == "structured"
    redacted = repr((lease, settlement))
    for secret in (
        pool._leases["structured"].owner_capability,
        pool._leases["structured"].publication_token,
    ):
        if secret is not None:
            assert secret not in redacted
    monkeypatch.setattr(owner, "join", lambda _timeout=None: True)
    pool.cancel("structured", "cleanup")
    pool.shutdown()


def test_claimed_artifact_diagnostics_record_consumer_without_claim_token(
    tmp_path,
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    claim_tokens = []

    def claim(completion):
        claim_tokens.append(completion.claim("diagnostic-consumer"))

    pool.set_completion_handler(claim)
    handle, token = make_ready(pool, resolver, backend)
    pool.reserve("claimed-diagnostic")
    pool.commit(
        "claimed-diagnostic",
        token,
        request(token, "claimed-diagnostic"),
    )
    clock.advance(0.05)
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    wait_until(lambda: pool.lease_snapshot("claimed-diagnostic").settled)

    snapshot = pool.lease_snapshot("claimed-diagnostic")
    assert snapshot.artifact_state is ArtifactState.CLAIMED
    assert snapshot.artifact_consumer_id == "diagnostic-consumer"
    assert claim_tokens[0] not in repr(snapshot)
    pool.shutdown()


def desired(hardware_id: str, *, rate: float = 100.0, channels: int = 1):
    return DesiredHardware(hardware_id, rate, channels)


def make_ready(pool, resolver, backend, hardware_id="mic-a"):
    resolver.devices[hardware_id] = device(hardware_id)
    result = pool.reconcile([desired(hardware_id)])
    assert result.accepted
    handle = backend.handles[hardware_id][-1]
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=10.0,
        adc_time=9.99,
        current_time=10.0,
    )
    assert pool.synchronize_stream(hardware_id, timeout=1.0)
    token = pool.ready_token(hardware_id)
    assert token is not None
    return handle, token


def request(token, run_id: str, *, duration: float = 0.05, trigger: float = 9.99):
    return CaptureRequest(
        run_id=run_id,
        trigger_monotonic=trigger,
        record_duration=duration,
        stream_token=token,
    )


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


OWNERLESS_FORBIDDEN_FIELDS = (
    "owner_instance_id",
    "owner",
    "writer",
    "stream",
    "artifact",
    "settlement_claim",
)


def test_pool_commit_ledger_backpressure_settles_exact_non_admission_with_safe_diagnostic(
    tmp_path, monkeypatch
):
    def one_entry_stream(**kwargs):
        return ResidentStream(owner_finality_limit=1, **kwargs)

    pool, resolver, backend, store, _ = make_pool(
        tmp_path, stream_factory=one_entry_stream
    )
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("ledger-holder")
    assert pool.commit(
        "ledger-holder",
        token,
        request(token, "ledger-holder", duration=0.5),
    ).state is LeaseState.COMMITTED
    stream = pool.stream_for_testing("mic-a")
    holder_quota = store.reserved_bytes
    original_remove = stream.remove_owner
    removal_calls = []

    def remove_spy(*args, **kwargs):
        removal_calls.append((args, kwargs))
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(stream, "remove_owner", remove_spy)
    pool.reserve("ledger-rejected")
    outcome = pool.commit(
        "ledger-rejected",
        token,
        request(token, "ledger-rejected", duration=0.5),
    )

    assert outcome.state is LeaseState.FAILED
    assert outcome.error_code is CaptureErrorCode.CAPTURE_CAPACITY_FULL
    assert pool.lease_snapshot("ledger-rejected").settled
    assert pool.snapshot().active_lease_count == 1
    assert store.reserved_bytes == holder_quota
    assert store.live_artifact_count == 1
    assert removal_calls == []
    rejected = pool._leases["ledger-rejected"]
    assert rejected.owner is None
    assert rejected.writer is None
    assert rejected.identity is None
    assert rejected.owner_instance_id is None
    assert rejected.owner_capability is None
    assert rejected.publication_token is None
    assert rejected.non_admission_proof is None
    assert rejected.commit_token is None
    assert rejected.settlement_claim is None
    assert rejected.terminal_summary.non_admission_confirmed
    assert (
        rejected.terminal_summary.owner_instance_id.run_id
        == "ledger-rejected"
    )
    assert (
        rejected.terminal_summary.intent
        is SettlementIntent.PREPARE_ROLLBACK
    )

    diagnostic = dict(outcome.diagnostic_context)
    assert diagnostic == {
        "capacity_domain": "owner_finality_ledger",
        "hardware_id": "mic-a",
        "ledger_limit": 1,
        **{
            f"ledger_{state.value.lower()}_count": (
                1 if state is OwnerFinalityState.ACTIVE else 0
            )
            for state in OwnerFinalityState
        },
        "backpressure_rejection_count": 1,
    }
    serialized = repr(outcome.diagnostic_context)
    assert "owner_finality_ledger" in serialized
    assert "ack_token" not in serialized
    assert "publication_token" not in serialized
    assert "secret" not in serialized
    assert "arbitrary-payload" not in serialized
    assert "CaptureOwner" not in serialized
    assert (
        pool._settlement_coordinator.diagnostics()
        .settlement_backpressure_rejections
        == 1
    )

    pool.cancel("ledger-holder", "cleanup")
    pool.shutdown()


def test_post_construction_ledger_backpressure_still_signals_joins_and_discards(
    tmp_path, monkeypatch
):
    def one_entry_stream(**kwargs):
        return ResidentStream(owner_finality_limit=1, **kwargs)

    pool, resolver, backend, store, _ = make_pool(
        tmp_path, stream_factory=one_entry_stream
    )
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("ledger-holder")
    assert pool.commit(
        "ledger-holder",
        token,
        request(token, "ledger-holder", duration=0.5),
    ).state is LeaseState.COMMITTED
    stream = pool.stream_for_testing("mic-a")
    holder_quota = store.reserved_bytes
    constructed_owners = []
    allocated_writers = []
    removal_calls = []
    original_allocate = store.allocate_writer

    monkeypatch.setattr(
        stream,
        "reserve_owner_prepare",
        lambda _identity: object(),
    )

    def reject_constructed(owner, identity, *, timeout):
        del timeout
        constructed_owners.append(owner)
        return stream._owner_finality.reserve_prepare(identity)

    def allocate_spy(**kwargs):
        writer = original_allocate(**kwargs)
        allocated_writers.append(writer)
        return writer

    monkeypatch.setattr(stream, "prepare_owner", reject_constructed)
    monkeypatch.setattr(store, "allocate_writer", allocate_spy)
    monkeypatch.setattr(
        stream,
        "remove_owner",
        lambda *args, **kwargs: removal_calls.append((args, kwargs)),
    )

    pool.reserve("ledger-constructed-rejection")
    outcome = pool.commit(
        "ledger-constructed-rejection",
        token,
        request(token, "ledger-constructed-rejection", duration=0.5),
    )

    assert outcome.state is LeaseState.FAILED
    assert outcome.error_code is CaptureErrorCode.CAPTURE_CAPACITY_FULL
    assert removal_calls == []
    assert len(constructed_owners) == 1
    assert len(allocated_writers) == 1
    assert constructed_owners[0].snapshot().state is CaptureOwnerState.FAILED
    assert constructed_owners[0].join(0)
    assert (
        store.state(allocated_writers[0].artifact_id)
        is ArtifactState.DELETED
    )
    assert store.reserved_bytes == holder_quota
    record = pool._leases["ledger-constructed-rejection"]
    assert record.settled
    assert record.settlement_claim is None
    assert record.terminal_summary.non_admission_confirmed
    assert (
        record.terminal_summary.intent
        is SettlementIntent.PREPARE_ROLLBACK
    )
    assert dict(outcome.diagnostic_context)[
        "backpressure_rejection_count"
    ] == 1

    pool.cancel("ledger-holder", "cleanup")
    pool.shutdown()


def test_terminal_claim_retirement_bounds_history_and_allows_run_id_reuse(
    tmp_path
):
    pool, resolver, backend, store, _ = make_pool(
        tmp_path, terminal_diagnostic_limit=1
    )
    _, token = make_ready(pool, resolver, backend)

    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    old_identity = pool._leases["run-a"].owner_instance_id
    old_owner = pool._leases["run-a"].owner
    old_owner_ref = weakref.ref(old_owner)
    del old_owner
    assert pool.cancel("run-a", "retire a").state is LeaseState.CANCELED
    assert pool._leases["run-a"].settlement_claim is None
    assert "run-a" not in pool._settlement_adapter._claims
    assert pool._leases["run-a"].owner is None
    assert pool._leases["run-a"].writer is None
    assert pool._leases["run-a"].stream is None
    assert pool._leases["run-a"].artifact is None
    gc.collect()
    assert old_owner_ref() is None

    pool.reserve("run-b")
    assert pool.commit(
        "run-b", token, request(token, "run-b", duration=0.5)
    ).state is LeaseState.COMMITTED
    assert pool.cancel("run-b", "retire b").state is LeaseState.CANCELED
    assert "run-a" not in pool._leases
    assert tuple(pool._terminal_order) == ("run-b",)
    assert len(pool._settlement_adapter._claims) == 0

    assert pool.reserve("run-a").state is LeaseState.RESERVED
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    assert pool._leases["run-a"].owner_instance_id != old_identity
    assert pool.cancel("run-a", "reuse a").state is LeaseState.CANCELED
    assert pool.lease_snapshot("run-a").settled
    assert len(pool._settlement_adapter._claims) == 0
    assert len(pool._terminal_order) == 1
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0
    pool.shutdown()


def test_pending_reference_clear_blocks_eviction_and_concurrent_retry_is_safe(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(
        tmp_path, terminal_diagnostic_limit=1
    )
    _, token = make_ready(pool, resolver, backend)
    for run_id in ("run-a", "run-b"):
        pool.reserve(run_id)
        assert pool.commit(
            run_id, token, request(token, run_id, duration=0.5)
        ).state is LeaseState.COMMITTED
    adapter = pool._settlement_adapter
    original_clear = adapter.clear_references
    clear_entered = threading.Event()
    clear_release = threading.Event()
    terminal_calls = []
    original_terminal = adapter.terminal_cas

    def blocked_clear(lease_id, *args, **kwargs):
        if lease_id == "run-a":
            clear_entered.set()
            assert clear_release.wait(2.0)
        return original_clear(lease_id, *args, **kwargs)

    def terminal_spy(claim, *args, **kwargs):
        terminal_calls.append(claim.lease_id)
        return original_terminal(claim, *args, **kwargs)

    monkeypatch.setattr(adapter, "clear_references", blocked_clear)
    monkeypatch.setattr(adapter, "terminal_cas", terminal_spy)
    cancel_results = []
    cancel_errors = []

    def cancel_a():
        try:
            cancel_results.append(pool.cancel("run-a", "blocked clear"))
        except Exception as error:
            cancel_errors.append(error)

    cancel_thread = threading.Thread(target=cancel_a)
    cancel_thread.start()
    assert clear_entered.wait(1.0)
    assert pool._leases["run-a"].settlement_claim is not None
    assert "run-a" in adapter._claims

    retry_started = threading.Event()
    retry_errors = []

    def retry():
        retry_started.set()
        try:
            pool.retry_settlements()
        except Exception as error:
            retry_errors.append(error)

    retry_thread = threading.Thread(target=retry)
    retry_thread.start()
    assert retry_started.wait(1.0)

    assert pool.cancel("run-b", "force overflow").state is LeaseState.CANCELED
    assert tuple(pool._terminal_order) == ("run-a",)
    assert "run-a" in pool._leases
    assert "run-b" not in pool._leases
    assert pool._leases["run-a"].owner is not None
    assert terminal_calls.count("run-a") == 1
    assert terminal_calls.count("run-b") == 1

    clear_release.set()
    cancel_thread.join(2.0)
    retry_thread.join(2.0)
    assert not cancel_thread.is_alive()
    assert not retry_thread.is_alive()
    assert cancel_errors == []
    assert retry_errors == []
    assert cancel_results[0].state is LeaseState.CANCELED
    assert "run-a" in pool._leases
    assert tuple(pool._terminal_order) == ("run-a",)
    assert len(adapter._claims) == 0
    assert terminal_calls.count("run-a") == 1
    assert terminal_calls.count("run-b") == 1
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0
    pool.shutdown()


def test_blocked_reference_clear_does_not_grow_terminal_diagnostics(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(
        tmp_path, terminal_diagnostic_limit=1
    )
    _, token = make_ready(pool, resolver, backend)
    blocked_run_id = "blocked-clear"
    pool.reserve(blocked_run_id)
    assert pool.commit(
        blocked_run_id,
        token,
        request(token, blocked_run_id, duration=0.5),
    ).state is LeaseState.COMMITTED
    adapter = pool._settlement_adapter
    original_clear = adapter.clear_references
    original_terminal = adapter.terminal_cas
    clear_entered = threading.Event()
    clear_release = threading.Event()
    terminal_calls = []
    cancel_errors = []

    def blocked_clear(lease_id, *args, **kwargs):
        if lease_id == blocked_run_id:
            clear_entered.set()
            assert clear_release.wait(3.0)
        return original_clear(lease_id, *args, **kwargs)

    def terminal_spy(claim, *args, **kwargs):
        terminal_calls.append(claim.lease_id)
        return original_terminal(claim, *args, **kwargs)

    monkeypatch.setattr(adapter, "clear_references", blocked_clear)
    monkeypatch.setattr(adapter, "terminal_cas", terminal_spy)

    def cancel_blocked():
        try:
            pool.cancel(blocked_run_id, "hold reference clear")
        except Exception as error:
            cancel_errors.append(error)

    cancel_thread = threading.Thread(target=cancel_blocked)
    cancel_thread.start()
    assert clear_entered.wait(1.0)
    old_identities = {}

    for index in range(8):
        run_id = f"later-{index}"
        pool.reserve(run_id)
        assert pool.commit(
            run_id, token, request(token, run_id, duration=0.5)
        ).state is LeaseState.COMMITTED
        old_identities[run_id] = pool._leases[run_id].owner_instance_id
        assert pool.cancel(run_id, "bounded diagnostic").state is LeaseState.CANCELED
        assert len(pool._terminal_order) <= 1
        assert pool.snapshot().terminal_diagnostic_count <= 1
        assert run_id not in pool._leases
        assert tuple(adapter._claims) == (blocked_run_id,)

    assert tuple(pool._leases) == (blocked_run_id,)
    assert terminal_calls.count(blocked_run_id) == 1
    clear_release.set()
    cancel_thread.join(3.0)
    assert not cancel_thread.is_alive()
    assert cancel_errors == []
    pool.retry_settlements()
    assert tuple(adapter._claims) == ()
    assert pool.snapshot().unsettled_transactions == 0
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0
    assert terminal_calls.count(blocked_run_id) == 1

    reused_run_id = "later-0"
    assert pool.reserve(reused_run_id).state is LeaseState.RESERVED
    assert pool.commit(
        reused_run_id,
        token,
        request(token, reused_run_id, duration=0.5),
    ).state is LeaseState.COMMITTED
    assert (
        pool._leases[reused_run_id].owner_instance_id
        != old_identities[reused_run_id]
    )
    assert pool.cancel(reused_run_id, "reuse evicted id").state is LeaseState.CANCELED
    assert terminal_calls.count(reused_run_id) == 2
    assert len(pool._terminal_order) == 1
    assert tuple(adapter._claims) == ()
    assert store.reserved_bytes == 0
    pool.shutdown()


def test_shutdown_stays_nonclosed_until_terminal_reference_clear_retries(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(
        tmp_path, terminal_diagnostic_limit=1
    )
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    adapter = pool._settlement_adapter
    original_clear = adapter.clear_references
    clear_entered = threading.Event()
    clear_release = threading.Event()

    def blocked_clear(lease_id, *args, **kwargs):
        clear_entered.set()
        assert clear_release.wait(2.0)
        return original_clear(lease_id, *args, **kwargs)

    monkeypatch.setattr(adapter, "clear_references", blocked_clear)
    cancel_thread = threading.Thread(
        target=lambda: pool.cancel("run-a", "blocked shutdown clear")
    )
    cancel_thread.start()
    assert clear_entered.wait(1.0)
    pool._shutdown_control_timeout = 0.03

    first = pool.shutdown()

    assert first.state is not PoolState.CLOSED
    assert "run-a" in adapter._claims
    clear_release.set()
    cancel_thread.join(2.0)
    assert not cancel_thread.is_alive()
    pool._shutdown_control_timeout = 1.0
    retried = pool.shutdown()
    assert retried.state is PoolState.CLOSED
    assert len(adapter._claims) == 0


def test_long_artifact_allocation_failure_is_bounded_and_settled(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    run_id = "long-artifact-error"
    secret = "secret-payload-" + ("x" * 350)
    pool.reserve(run_id)
    stream = pool.stream_for_testing("mic-a")

    def fail_allocation(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(store, "allocate_writer", fail_allocation)

    outcome = pool.commit(
        run_id, token, request(token, run_id, duration=0.5)
    )

    assert outcome.state is LeaseState.FAILED
    assert outcome.error_code is CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
    assert pool.lease_snapshot(run_id).state is not LeaseState.COMMITTING
    record = pool._leases[run_id]
    assert record.settled
    assert record.settlement_claim is None
    assert run_id not in pool._settlement_adapter._claims
    assert secret not in repr(
        (
            outcome,
            record.terminal_summary,
            pool._settlement_coordinator.diagnostics(),
        )
    )
    assert stream.finality_snapshot().entries == ()
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0
    assert pool.shutdown().state is PoolState.CLOSED


def test_stream_failure_during_writer_allocation_disposes_late_writer(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    run_id = "allocation-stream-failure"
    pool.reserve(run_id)
    stream = pool.stream_for_testing("mic-a")
    allocation_entered = threading.Event()
    allocation_release = threading.Event()
    original_allocate = store.allocate_writer
    allocated_writers = []
    commit_outcomes = []
    commit_errors = []

    def blocked_allocation(**kwargs):
        allocation_entered.set()
        assert allocation_release.wait(3.0)
        writer = original_allocate(**kwargs)
        allocated_writers.append(writer)
        return writer

    def commit_capture():
        try:
            commit_outcomes.append(
                pool.commit(
                    run_id,
                    token,
                    request(token, run_id, duration=0.5),
                )
            )
        except Exception as error:
            commit_errors.append(error)

    monkeypatch.setattr(store, "allocate_writer", blocked_allocation)
    commit_thread = threading.Thread(target=commit_capture)
    commit_thread.start()
    assert allocation_entered.wait(1.0)
    record = pool._leases[run_id]
    assert record.state is LeaseState.COMMITTING
    assert record.owner is None
    assert record.writer is None

    assert stream._fail_generation(
        CaptureErrorCode.STREAM_OPEN_FAILED,
        "failure while writer allocation is blocked",
    )
    wait_until(
        lambda: (
            record.settlement_claim is not None
            or record.state is LeaseState.FAILED
        )
    )
    allocation_release.set()
    commit_thread.join(3.0)

    assert not commit_thread.is_alive()
    assert commit_errors == []
    assert len(commit_outcomes) == 1
    for _ in range(8):
        if pool.lease_snapshot(run_id).settled:
            break
        pool.retry_settlements()
    outcome = pool.lease_outcome(run_id)
    assert outcome.state is LeaseState.FAILED
    assert outcome.error_code is CaptureErrorCode.STREAM_OPEN_FAILED
    assert record.terminal_summary.intent is SettlementIntent.STREAM_FAILURE
    assert len(allocated_writers) == 1
    assert (
        store.state(allocated_writers[0].artifact_id)
        is ArtifactState.DELETED
    )
    assert store.reserved_bytes == 0
    assert pool.snapshot().active_lease_count == 0
    assert pool.snapshot().unsettled_transactions == 0
    assert record.settlement_claim is None
    assert run_id not in pool._settlement_adapter._claims
    pool.shutdown()


def test_reserve_validates_owner_identity_run_id_length_before_mutation(
    tmp_path,
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    maximum_run_id = "r" * 256
    assert pool.reserve(maximum_run_id).state is LeaseState.RESERVED
    assert (
        pool.cancel(maximum_run_id, "boundary cleanup").state
        is LeaseState.CANCELED
    )
    assert pool.lease_snapshot(maximum_run_id).settled
    baseline = (
        tuple(pool._leases),
        tuple(pool._terminal_order),
        tuple(pool._settlement_adapter._claims),
        pool.snapshot(),
        store.reserved_bytes,
    )
    oversized_run_id = "r" * 257

    with pytest.raises(ValueError, match="at most 256"):
        pool.reserve(oversized_run_id)

    assert oversized_run_id not in pool._leases
    assert (
        tuple(pool._leases),
        tuple(pool._terminal_order),
        tuple(pool._settlement_adapter._claims),
        pool.snapshot(),
        store.reserved_bytes,
    ) == baseline
    pool.shutdown()


def test_reserve_rejects_stateful_str_subclass_before_any_mutation(
    tmp_path,
):
    class StatefulRunId(str):
        def __new__(cls, value):
            instance = super().__new__(cls, value)
            instance.arbitrary_payload = object()
            return instance

    pool, _, _, store, _ = make_pool(tmp_path)
    run_id = StatefulRunId("stateful-run-id")
    baseline = (
        tuple(pool._leases),
        tuple(pool._terminal_order),
        tuple(pool._settlement_adapter._claims),
        pool.snapshot(),
        store.reserved_bytes,
    )

    with pytest.raises(ValueError, match="run_id"):
        pool.reserve(run_id)

    assert run_id not in pool._leases
    assert (
        tuple(pool._leases),
        tuple(pool._terminal_order),
        tuple(pool._settlement_adapter._claims),
        pool.snapshot(),
        store.reserved_bytes,
    ) == baseline
    pool.shutdown()


@pytest.mark.parametrize(
    "entry_point",
    (
        "commit",
        "cancel",
        "release",
        "lease_outcome",
        "lease_snapshot",
    ),
)
def test_public_run_id_entry_points_reject_str_subclasses_before_lookup(
    tmp_path, entry_point
):
    class StatefulRunId(str):
        def __new__(cls, value):
            instance = super().__new__(cls, value)
            instance.hash_calls = 0
            instance.equality_calls = 0
            instance.arbitrary_payload = object()
            return instance

        def __hash__(self):
            self.hash_calls += 1
            return super().__hash__()

        def __eq__(self, other):
            self.equality_calls += 1
            return super().__eq__(other)

    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    canonical_run_id = "canonical-run"
    assert pool.reserve(canonical_run_id).state is LeaseState.RESERVED
    run_id = StatefulRunId(canonical_run_id)
    baseline = (
        tuple(pool._leases),
        tuple(pool._terminal_order),
        tuple(pool._settlement_adapter._claims),
        pool.snapshot(),
        store.reserved_bytes,
    )
    invocations = {
        "commit": lambda: pool.commit(
            run_id,
            token,
            request(token, canonical_run_id, duration=0.5),
        ),
        "cancel": lambda: pool.cancel(run_id, "invalid subclass"),
        "release": lambda: pool.release(run_id, "capability", "artifact"),
        "lease_outcome": lambda: pool.lease_outcome(run_id),
        "lease_snapshot": lambda: pool.lease_snapshot(run_id),
    }

    try:
        with pytest.raises(ValueError, match="run_id"):
            invocations[entry_point]()

        assert run_id.hash_calls == 0
        assert run_id.equality_calls == 0
        assert (
            tuple(pool._leases),
            tuple(pool._terminal_order),
            tuple(pool._settlement_adapter._claims),
            pool.snapshot(),
            store.reserved_bytes,
        ) == baseline
    finally:
        pool.shutdown()


@pytest.mark.parametrize(
    "mismatch",
    [
        "pool_epoch",
        "hardware_id",
        "stream_generation",
        "run_id",
        "commit_attempt_id",
        "owner_capability",
        "instance_nonce",
        "alternate_identity",
    ],
)
def test_stale_run_identity_cannot_mutate_active_replacement_lease(
    tmp_path, mismatch
):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    handle, token = make_ready(pool, resolver, backend)
    for run_id in ("run-a", "run-b"):
        pool.reserve(run_id)
        assert pool.commit(
            run_id,
            token,
            request(token, run_id, duration=0.5),
        ).state is LeaseState.COMMITTED
    record_a = pool._leases["run-a"]
    record_b = pool._leases["run-b"]
    stream = record_a.stream
    wait_until(
        lambda: (
            record_a.owner.snapshot().state is CaptureOwnerState.LIVE
            and record_b.owner.snapshot().state is CaptureOwnerState.LIVE
        )
    )
    stale_id = record_a.owner_instance_id
    replacement_id = record_b.owner_instance_id
    if mismatch == "alternate_identity":
        candidate = OwnerInstanceId(
            pool_epoch=pool.pool_epoch,
            hardware_id="mic-a",
            stream_generation=token.stream_generation,
            run_id="run-other",
            commit_attempt_id="attempt-other",
            owner_capability="cap-other",
            instance_nonce="nonce-other",
        )
    else:
        candidate = replace(
            stale_id,
            **{
                mismatch: (
                    token.stream_generation + 1
                    if mismatch == "stream_generation"
                    else f"wrong-{mismatch}"
                )
            },
        )
    replacement_registration = stream._registrations[replacement_id]
    ring_before = stream._ring
    callback_before = stream._callback
    artifact_before = record_b.writer.artifact_id
    lease_before = pool.lease_snapshot("run-b")
    capacity_before = pool.snapshot().active_lease_count
    quota_before = store.reserved_bytes
    owner_progress_before = record_b.owner.snapshot()
    stream_before = stream.snapshot()
    finality_before = stream.finality_snapshot()

    receipt = stream.remove_owner(candidate, timeout=1.0)

    assert receipt.outcome is ValueOwnerRemovalOutcome.MISMATCH
    assert stream._registrations[replacement_id] is replacement_registration
    assert stream._ring is ring_before
    assert stream._callback is callback_before
    assert record_b.writer.artifact_id == artifact_before
    assert pool.lease_snapshot("run-b") == lease_before
    assert pool.snapshot().active_lease_count == capacity_before
    assert store.reserved_bytes == quota_before
    assert record_b.owner.snapshot() == owner_progress_before
    assert stream.snapshot() == stream_before
    assert stream.finality_snapshot() == finality_before

    clock.advance(0.05)
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert pool.synchronize_stream("mic-a", timeout=1.0)
    assert stream.snapshot().cursor > stream_before.cursor
    assert stream._registrations[replacement_id] is replacement_registration

    assert (
        pool.cancel("run-a", "exact stale settlement").state
        is LeaseState.CANCELED
    )
    assert pool.lease_snapshot("run-a").settled
    assert pool.lease_snapshot("run-b").state is LeaseState.COMMITTED
    assert pool.snapshot().active_lease_count == 1

    later_owner = SimpleNamespace(
        pool_epoch=pool.pool_epoch,
        stream_generation=token.stream_generation,
        run_id="run-a",
        commit_attempt_id="attempt-a-later",
        owner_capability="cap-a-later",
    )
    later_id = OwnerInstanceId(
        pool_epoch=pool.pool_epoch,
        hardware_id="mic-a",
        stream_generation=token.stream_generation,
        run_id="run-a",
        commit_attempt_id=later_owner.commit_attempt_id,
        owner_capability=later_owner.owner_capability,
        instance_nonce="nonce-a-later",
    )
    assert stream.prepare_owner(later_owner, later_id, timeout=1.0)
    assert replacement_id in stream._registrations
    assert later_id in stream._registrations
    assert stream.remove_owner(later_id, timeout=1.0).confirms(later_id)

    pool.cancel("run-b", "cleanup")
    pool.shutdown()


@pytest.mark.parametrize(
    "transition", ["validation_failure", "client_cancel"]
)
@pytest.mark.parametrize("forbidden_field", OWNERLESS_FORBIDDEN_FIELDS)
def test_ownerless_reserved_transition_rejects_each_forbidden_resource_independently(
    tmp_path, transition, forbidden_field
):
    pool, _, _, store, _ = make_pool(tmp_path)
    sentinel = object()
    record = SimpleNamespace(
        run_id="ownerless-audit",
        state=LeaseState.RESERVED,
        outcome=None,
        settled=False,
        owner_instance_id=None,
        owner=None,
        writer=None,
        stream=None,
        artifact=None,
        artifact_id=None,
        settlement_claim=None,
    )
    setattr(record, forbidden_field, sentinel)
    with pool._lock:
        pool._leases[record.run_id] = record
    before_capacity = pool.snapshot().active_lease_count
    before_terminals = len(pool._terminal_order)
    before_quota = store.reserved_bytes

    def invoke():
        helper = getattr(pool, "_ownerless_reserved_terminal_locked", None)
        state = (
            LeaseState.FAILED
            if transition == "validation_failure"
            else LeaseState.CANCELED
        )
        code = (
            CaptureErrorCode.INVALID_CAPTURE_REQUEST
            if transition == "validation_failure"
            else CaptureErrorCode.CAPTURE_CANCELED
        )
        with pool._lock:
            if helper is not None:
                helper(record, state, code, transition)
            else:
                pool._terminalize_locked(record, state, code, transition)

    with pytest.raises(AssertionError):
        invoke()

    assert record.state is LeaseState.RESERVED
    assert record.settled is False
    assert pool.snapshot().active_lease_count == before_capacity
    assert len(pool._terminal_order) == before_terminals
    assert store.reserved_bytes == before_quota


REAL_SETTLEMENT_INTENTS = (
    "PREPARE_ROLLBACK",
    "CLIENT_CANCEL",
    "OWNER_SUCCESS",
    "OWNER_FAILURE",
    "STREAM_FAILURE",
    "POOL_SHUTDOWN",
)

REAL_REMOVAL_RESULTS = (
    "REMOVED",
    "TOMBSTONED_ABSENT",
    "MISMATCH",
    "INDETERMINATE",
    "EXCEPTION",
    "TIMEOUT",
)


@pytest.mark.parametrize(
    "removal_result",
    REAL_REMOVAL_RESULTS,
)
@pytest.mark.parametrize("intent", REAL_SETTLEMENT_INTENTS)
def test_real_entry_points_obey_full_intent_by_removal_outcome_matrix(
    tmp_path, monkeypatch, intent, removal_result
):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    completions = []
    pool.set_completion_handler(completions.append)
    handle, token = make_ready(pool, resolver, backend)
    run_id = f"matrix-{intent.lower()}-{removal_result.lower()}"
    pool.reserve(run_id)
    assert pool.commit(
        run_id,
        token,
        request(
            token,
            run_id,
            duration=0.05 if intent == "OWNER_SUCCESS" else 0.5,
        ),
    ).state is LeaseState.COMMITTED
    record = pool._leases[run_id]
    expected_owner_instance_id = record.owner_instance_id
    stream = record.stream
    original_remove = stream.remove_owner
    removal_calls = []
    adapter = pool._settlement_adapter
    later_calls = {
        "signal": 0,
        "join": 0,
        "readiness": 0,
        "acknowledgement": 0,
        "prune": 0,
        "artifact_publication": 0,
        "quota": 0,
        "terminal": 0,
    }
    terminal_claims = []
    originals = {
        name: getattr(adapter, name)
        for name in (
            "signal_owner",
            "join_owner",
            "check_artifact_readiness",
            "acknowledge_finality",
            "prune_finality",
            "settle_artifact_and_publication",
            "confirm_quota_outcome",
            "terminal_cas",
        )
    }
    quota_before = store.reserved_bytes
    original_state = record.state

    def injected_remove(*args, **kwargs):
        removal_calls.append((args, kwargs))
        instance_id = args[0]
        if removal_result == "REMOVED":
            return original_remove(*args, **kwargs)
        if removal_result == "EXCEPTION":
            raise RuntimeError("injected removal exception")
        if removal_result == "TIMEOUT":
            raise TimeoutError("injected removal timeout")
        return ValueOwnerRemovalReceipt(
            instance_id,
            ValueOwnerRemovalOutcome(removal_result),
        )

    def counted(name, key):
        original = originals[name]

        def invoke(*args, **kwargs):
            later_calls[key] += 1
            if name == "terminal_cas":
                terminal_claims.append(args[0])
            if (
                removal_result == "TOMBSTONED_ABSENT"
                and name == "acknowledge_finality"
            ):
                claim = args[0]
                return FinalityAckReceipt(
                    claim.stream_instance_id,
                    claim.owner_instance_id,
                    1,
                    "matrix-exact-ack",
                )
            if (
                removal_result == "TOMBSTONED_ABSENT"
                and name == "prune_finality"
            ):
                claim = args[0]
                return FinalityPruneReceipt(
                    claim.owner_instance_id,
                    FinalityPruneOutcome.PRUNED,
                )
            return original(*args, **kwargs)

        return invoke

    monkeypatch.setattr(stream, "remove_owner", injected_remove)
    for name, key in (
        ("signal_owner", "signal"),
        ("join_owner", "join"),
        ("check_artifact_readiness", "readiness"),
        ("acknowledge_finality", "acknowledgement"),
        ("prune_finality", "prune"),
        ("settle_artifact_and_publication", "artifact_publication"),
        ("confirm_quota_outcome", "quota"),
        ("terminal_cas", "terminal"),
    ):
        monkeypatch.setattr(adapter, name, counted(name, key))

    if intent == "PREPARE_ROLLBACK":
        record.state = LeaseState.COMMITTING
        record.settled = False
        pool._rollback_commit(
            record,
            record.owner,
            record.writer,
            record.stream,
            record.identity,
            True,
            CaptureErrorCode.CAPTURE_CANCELED,
            "matrix rollback",
        )
    elif intent == "CLIENT_CANCEL":
        pool.cancel(run_id, "matrix cancel")
    elif intent == "OWNER_SUCCESS":
        clock.advance(0.05)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        pool.synchronize_stream("mic-a", timeout=1.0)
        wait_until(lambda: removal_calls)
    elif intent == "OWNER_FAILURE":
        record.owner.fail(
            CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED,
            "matrix owner failure",
        )
        wait_until(lambda: removal_calls)
    elif intent == "STREAM_FAILURE":
        stream.report_backend_failure(
            RuntimeError("matrix stream failure"),
            timeout=1.0,
        )
        wait_until(lambda: removal_calls)
    else:
        pool._shutdown_control_timeout = 0.05
        pool.shutdown()

    if removal_result in ("REMOVED", "TOMBSTONED_ABSENT"):
        for _ in range(4):
            if pool.lease_snapshot(run_id).settled:
                break
            pool.retry_settlements()

    try:
        lease = pool.lease_snapshot(run_id)
        assert removal_calls, f"{intent} bypassed exact owner removal"
        claim = getattr(record, "settlement_claim", None)
        if removal_result in ("REMOVED", "TOMBSTONED_ABSENT"):
            assert claim is None
            assert terminal_claims, f"{intent} never reached terminal CAS"
            claim = terminal_claims[-1]
            assert record.terminal_summary.intent.value == intent
            assert lease.settled, (
                claim.progress.phase,
                claim.progress.owner_joined,
                claim.progress.finality_pruned,
                claim.progress.publication_resolved,
                claim.progress.artifact_disposition_confirmed,
                store.state(record.writer.artifact_id),
                later_calls,
            )
            assert pool.snapshot().active_lease_count == 0
            assert claim.progress.removal_receipt is not None
            assert claim.progress.removal_receipt.confirms(
                expected_owner_instance_id
            )
            assert record.identity is None
            assert record.owner_instance_id is None
            assert record.owner_capability is None
            assert record.publication_token is None
            assert record.non_admission_proof is None
            assert record.commit_token is None
            assert (
                record.terminal_summary.owner_instance_id.run_id
                == expected_owner_instance_id.run_id
            )
            assert later_calls["signal"] == 1
            assert later_calls["join"] >= 1
            expected_calls = {
                "readiness": 1,
                "acknowledgement": 1,
                "prune": 1,
                "artifact_publication": (
                    2 if intent == "OWNER_SUCCESS" else 1
                ),
                "quota": 1,
                "terminal": 1,
            }
            assert all(
                later_calls[key] == expected
                for key, expected in expected_calls.items()
            ), (intent, removal_result, later_calls)
        else:
            assert claim is not None, (
                f"{intent} never retained a retryable settlement claim"
            )
            assert claim.intent.value == intent
            expected_state = (
                LeaseState.COMMITTING
                if intent == "PREPARE_ROLLBACK"
                else original_state
            )
            assert lease.state is expected_state
            assert lease.settled is False
            assert pool.snapshot().active_lease_count == 1
            assert completions == []
            assert store.reserved_bytes == quota_before
            assert later_calls == {
                key: 0 for key in later_calls
            }, (intent, removal_result, later_calls)
            assert claim.progress.removal_receipt is None
    finally:
        monkeypatch.setattr(stream, "remove_owner", original_remove)
        for name, original in originals.items():
            monkeypatch.setattr(adapter, name, original)
        if (
            removal_result == "TOMBSTONED_ABSENT"
            and stream.snapshot().state is not StreamState.CLOSED
        ):
                original_remove(expected_owner_instance_id, timeout=1.0)
        if pool.snapshot().state is PoolState.OPEN:
            pool.cancel(run_id, "matrix cleanup")
        pool.shutdown()


SHUTDOWN_SETTLEMENT_BLOCKERS = (
    "removal",
    "join",
    "acknowledgement",
    "prune",
    "publication",
    "artifact_disposition",
)


@pytest.mark.parametrize("blocked_stage", SHUTDOWN_SETTLEMENT_BLOCKERS)
def test_shutdown_waits_for_each_settlement_blocker_and_management_retry_is_business_isolated(
    tmp_path, monkeypatch, blocked_stage
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("shutdown-matrix")
    assert pool.commit(
        "shutdown-matrix",
        token,
        request(token, "shutdown-matrix", duration=0.5),
    ).state is LeaseState.COMMITTED
    record = pool._leases["shutdown-matrix"]
    stream = record.stream
    owner = record.owner
    writer = record.writer
    blocked = [True]
    blocker_calls = []

    if blocked_stage == "removal":
        original = stream.remove_owner

        def removal(*args, **kwargs):
            blocker_calls.append("removal")
            if blocked[0]:
                instance_id = args[-1]
                return ValueOwnerRemovalReceipt(
                    instance_id,
                    ValueOwnerRemovalOutcome.INDETERMINATE,
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(stream, "remove_owner", removal)
    elif blocked_stage == "join":
        original = owner.join

        def join(*args, **kwargs):
            blocker_calls.append("join")
            return False if blocked[0] else original(*args, **kwargs)

        monkeypatch.setattr(owner, "join", join)
    elif blocked_stage == "artifact_disposition":
        original = writer.discard

        def discard(*args, **kwargs):
            blocker_calls.append("artifact_disposition")
            return False if blocked[0] else original(*args, **kwargs)

        monkeypatch.setattr(writer, "discard", discard)
    elif blocked_stage in ("acknowledgement", "prune"):
        attribute = (
            "acknowledge_finality"
            if blocked_stage == "acknowledgement"
            else "prune_finality"
        )
        original = getattr(stream, attribute, None)

        def finality_step(*args, **kwargs):
            blocker_calls.append(blocked_stage)
            if blocked[0]:
                return None
            return None if original is None else original(*args, **kwargs)

        monkeypatch.setattr(stream, attribute, finality_step, raising=False)
    else:
        adapter = pool._settlement_adapter
        original = adapter.settle_artifact_and_publication

        def publication(*args, **kwargs):
            blocker_calls.append("publication")
            if blocked[0]:
                return ArtifactSettlementResult(False, False)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            adapter, "settle_artifact_and_publication", publication
        )

    pool._shutdown_control_timeout = 0.03
    first = pool.shutdown()
    first_lease_settled = pool.lease_snapshot("shutdown-matrix").settled

    business_calls = {
        "reserve": 0,
        "commit": 0,
        "reconcile": 0,
        "stream_open": 0,
        "capture": 0,
        "owner_prepare": 0,
    }

    def spy(target, attribute, key):
        original_method = getattr(target, attribute)

        def wrapped(*args, **kwargs):
            business_calls[key] += 1
            return original_method(*args, **kwargs)

        monkeypatch.setattr(target, attribute, wrapped)

    spy(pool, "reserve", "reserve")
    spy(pool, "commit", "commit")
    spy(pool, "reconcile", "reconcile")
    spy(backend, "open_input", "stream_open")
    spy(stream, "submit_owner_control", "capture")
    spy(stream, "prepare_owner", "owner_prepare")
    blocked[0] = False
    pool._shutdown_control_timeout = 1.0

    retried = pool.shutdown()

    assert business_calls == {key: 0 for key in business_calls}
    assert blocker_calls, (
        f"shutdown skipped the {blocked_stage} settlement prerequisite"
    )
    assert first.state is not PoolState.CLOSED, (
        f"pool closed while {blocked_stage} was non-confirming"
    )
    assert not first_lease_settled
    assert retried.state is PoolState.CLOSED
    assert pool.lease_snapshot("shutdown-matrix").settled


def test_desired_dedup_conflict_and_atomic_seven_rejection(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices["mic-a"] = device("mic-a")
    try:
        accepted = pool.reconcile([desired("mic-a"), desired("mic-a")])
        assert accepted.accepted
        assert backend.open_order == ["mic-a"]
        assert pool.snapshot().desired_hardware_ids == ("mic-a",)

        conflict = pool.reconcile(
            [desired("mic-a"), desired("mic-a", rate=200.0)]
        )
        assert not conflict.accepted
        assert conflict.error_code is CaptureErrorCode.HARDWARE_RESOLUTION_CONFLICT
        assert pool.snapshot().desired_hardware_ids == ("mic-a",)

        too_many = pool.reconcile([desired(f"mic-{index}") for index in range(7)])
        assert not too_many.accepted
        assert too_many.error_code is CaptureErrorCode.TOO_MANY_REQUIRED_HARDWARE
        assert pool.snapshot().desired_hardware_ids == ("mic-a",)
    finally:
        pool.shutdown()


def test_reconcile_add_before_remove_when_capacity_permits_and_swap_at_six(tmp_path):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    for index in range(7):
        hardware_id = f"mic-{index}"
        resolver.devices[hardware_id] = device(hardware_id, index=index)
    try:
        assert pool.reconcile([desired("mic-0")]).accepted
        backend.open_order.clear()
        backend.close_order.clear()
        results = []
        worker = threading.Thread(
            target=lambda: results.append(pool.reconcile([desired("mic-1")]))
        )
        worker.start()
        wait_until(lambda: backend.open_order == ["mic-1"])
        backend.handles["mic-1"][0].emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        worker.join(2.0)
        assert results[0].accepted
        assert backend.open_order == ["mic-1"]
        assert backend.close_order == ["mic-0"]

        assert pool.reconcile([desired(f"mic-{index}") for index in range(1, 7)]).accepted
        backend.open_order.clear()
        backend.close_order.clear()
        assert pool.reconcile(
            [desired(f"mic-{index}") for index in range(7) if index != 1]
        ).accepted
        assert backend.close_order[0] == "mic-1"
        assert backend.open_order[0] == "mic-0"
    finally:
        pool.shutdown()


def test_six_for_six_close_failure_blocks_opens_and_same_desired_retries(
    tmp_path,
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    old_ids = [f"old-{index}" for index in range(6)]
    new_ids = [f"new-{index}" for index in range(6)]
    for index, hardware_id in enumerate(old_ids + new_ids):
        resolver.devices[hardware_id] = device(hardware_id, index=index)
    old_desired = [desired(hardware_id) for hardware_id in old_ids]
    new_desired = [desired(hardware_id) for hardware_id in new_ids]
    try:
        assert pool.reconcile(old_desired).accepted
        for hardware_id in old_ids:
            backend.handles[hardware_id][0].emit(
                frames=5,
                channels=1,
                callback_monotonic=clock(),
                adc_time=clock() - 0.01,
                current_time=clock(),
            )
            assert pool.synchronize_stream(hardware_id, timeout=1.0)
        failed_stream = pool.stream_for_testing("old-0")
        failed_handle = backend.handles["old-0"][0]
        failed_handle.close_errors.append(OSError("close failed"))

        assert pool.reconcile(new_desired).accepted
        assert all(not backend.handles[item] for item in new_ids)
        assert sum(
            not handle.physically_closed
            for handles in backend.handles.values()
            for handle in handles
        ) <= 6
        assert pool._closing_streams["old-0"].record.stream is failed_stream
        assert all(
            pool.snapshot().stream_for(item).state is StreamState.FAILED
            for item in new_ids
        )

        assert pool.reconcile(new_desired).accepted
        assert failed_handle.physically_closed
        assert all(len(backend.handles[item]) == 1 for item in new_ids)
        assert sum(
            not handle.physically_closed
            for handles in backend.handles.values()
            for handle in handles
        ) == 6
        assert pool._closing_streams == {}
    finally:
        pool.shutdown()


def test_add_before_remove_keeps_old_stream_until_replacement_is_ready(tmp_path):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    results = []
    worker = threading.Thread(
        target=lambda: results.append(pool.reconcile([desired("mic-b")]))
    )
    worker.start()
    wait_until(lambda: len(backend.handles["mic-b"]) == 1)
    old_handle = backend.handles["mic-a"][0]
    time.sleep(0.05)
    assert worker.is_alive()
    assert not old_handle.physically_closed

    backend.handles["mic-b"][0].emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    worker.join(2.0)
    try:
        assert results[0].accepted
        assert old_handle.physically_closed
        assert pool.snapshot().stream_for("mic-b").state is StreamState.READY
    finally:
        pool.shutdown()


def test_late_ready_after_reconcile_timeout_closes_exact_obsolete_stream(tmp_path):
    pool, resolver, backend, _, clock = make_pool(
        tmp_path,
        reconcile_ready_timeout=0.01,
    )
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    try:
        assert pool.reconcile([desired("mic-b")]).accepted
        assert pool.snapshot().published_hardware_ids == ("mic-a", "mic-b")
        assert pool.reconcile([desired("mic-b")]).accepted

        backend.handles["mic-b"][0].emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )

        wait_until(
            lambda: (
                pool.snapshot().published_hardware_ids == ("mic-b",)
                and backend.handles["mic-a"][0].physically_closed
            )
        )
        assert not backend.handles["mic-b"][0].physically_closed
        assert backend.close_threads[-1] == "resident-audio-pool-control"
        assert pool.ready_token("mic-b") is not None
    finally:
        pool.shutdown()


def test_late_ready_pending_removal_is_canceled_by_superseding_desired(tmp_path):
    pool, resolver, backend, _, clock = make_pool(
        tmp_path,
        reconcile_ready_timeout=0.01,
    )
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    try:
        assert pool.reconcile([desired("mic-b")]).accepted
        assert pool.reconcile([desired("mic-a")]).accepted
        assert pool.snapshot().published_hardware_ids == ("mic-a",)
        assert backend.handles["mic-b"][0].physically_closed

        backend.handles["mic-b"][0].emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        time.sleep(0.02)
        assert not backend.handles["mic-a"][0].physically_closed
        assert pool.ready_token("mic-a") is not None
    finally:
        pool.shutdown()


def test_failed_replacement_settles_pending_removal_without_closing_old(tmp_path):
    pool, resolver, backend, _, _ = make_pool(
        tmp_path,
        reconcile_ready_timeout=0.01,
    )
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    try:
        assert pool.reconcile([desired("mic-b")]).accepted
        replacement = pool.stream_for_testing("mic-b")
        replacement._fail_generation(
            CaptureErrorCode.INPUT_DATA_TIMEOUT,
            "replacement failed before READY",
        )
        wait_until(
            lambda: pool.snapshot().stream_for("mic-b").state
            is StreamState.FAILED
        )
        assert not backend.handles["mic-a"][0].physically_closed
        assert pool._pending_removals == []
    finally:
        pool.shutdown()


def test_shutdown_settles_late_ready_pending_removal_without_double_close(tmp_path):
    pool, resolver, backend, _, clock = make_pool(
        tmp_path,
        reconcile_ready_timeout=0.01,
    )
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    assert pool.reconcile([desired("mic-b")]).accepted

    snapshot = pool.shutdown()
    backend.handles["mic-b"][0].emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )

    assert snapshot.state is PoolState.CLOSED
    assert pool._pending_removals == []
    assert backend.handles["mic-a"][0].calls.count("close") == 1
    assert backend.handles["mic-b"][0].calls.count("close") == 1


def test_shutdown_does_not_reclose_blocked_pending_removal_worker(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, clock = make_pool(
        tmp_path,
        reconcile_ready_timeout=0.01,
    )
    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.02
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    old_stream = pool.stream_for_testing("mic-a")
    original_close = old_stream.close
    close_entered = threading.Event()
    close_continue = threading.Event()
    close_calls = 0

    def blocked_close(*args, **kwargs):
        nonlocal close_calls
        close_calls += 1
        close_entered.set()
        assert close_continue.wait(2.0)
        return original_close(*args, **kwargs)

    monkeypatch.setattr(old_stream, "close", blocked_close)
    assert pool.reconcile([desired("mic-b")]).accepted
    backend.handles["mic-b"][0].emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert close_entered.wait(1.0)

    results = []
    shutdown = threading.Thread(target=lambda: results.append(pool.shutdown()))
    shutdown.start()
    shutdown.join(0.2)
    try:
        assert not shutdown.is_alive()
        assert results[0].state is PoolState.FAILED_CLOSED
        assert close_calls == 1
        assert pool._closing_streams["mic-a"].record.stream is old_stream
    finally:
        close_continue.set()
        shutdown.join(2.0)
        wait_until(lambda: backend.handles["mic-a"][0].physically_closed)


def test_stale_removal_action_cannot_close_hardware_readded_by_new_reconcile(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-b"] = device("mic-b")
    old_stream = pool.stream_for_testing("mic-a")
    original_close = old_stream.close
    stale_close_entered = threading.Event()
    stale_close_continue = threading.Event()

    def blocked_old_close(*args, **kwargs):
        stale_close_entered.set()
        assert stale_close_continue.wait(2.0)
        return original_close(*args, **kwargs)

    monkeypatch.setattr(old_stream, "close", blocked_old_close)
    first = threading.Thread(target=lambda: pool.reconcile([desired("mic-b")]))
    first.start()
    wait_until(lambda: len(backend.handles["mic-b"]) == 1)
    backend.handles["mic-b"][0].emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert stale_close_entered.wait(1.0)

    second_results = []
    second = threading.Thread(
        target=lambda: second_results.append(pool.reconcile([desired("mic-a")]))
    )
    second.start()
    time.sleep(0.05)
    assert second.is_alive()
    assert len(backend.handles["mic-a"]) == 1
    stale_close_continue.set()
    wait_until(lambda: len(backend.handles["mic-a"]) == 2)
    backend.handles["mic-a"][1].emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    first.join(2.0)
    second.join(2.0)
    try:
        assert second_results[0].accepted
        assert "mic-a" in pool.snapshot().published_hardware_ids
        assert pool.ready_token("mic-a") is not None
        assert backend.handles["mic-a"][0].physically_closed
        assert not backend.handles["mic-a"][1].physically_closed
    finally:
        pool.shutdown()


def test_same_hardware_format_change_replaces_stream_and_publishes_new_ready_format(
    tmp_path,
):
    pool, resolver, backend, _, clock = make_pool(tmp_path)
    make_ready(pool, resolver, backend, "mic-a")
    resolver.devices["mic-a"] = device("mic-a", rate=200.0, channels=1)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            pool.reconcile([desired("mic-a", rate=200.0)])
        )
    )
    worker.start()
    wait_until(lambda: len(backend.handles["mic-a"]) == 2)
    new_handle = backend.handles["mic-a"][1]
    new_handle.emit(
        frames=10,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    worker.join(2.0)
    try:
        token = pool.ready_token("mic-a")
        assert results[0].accepted
        assert token is not None
        assert token.sample_rate == 200.0
        assert token.stream_generation == 2
        assert backend.close_order[0] == "mic-a"
    finally:
        pool.shutdown()


def test_open_failure_isolated_and_duplicate_reconcile_does_not_reopen(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices.update({"good": device("good"), "bad": device("bad")})
    backend.failures["bad"] = OSError("unplugged")
    try:
        result = pool.reconcile([desired("good"), desired("bad")])
        assert result.accepted
        snapshots = {item.hardware_id: item for item in pool.snapshot().streams}
        assert snapshots["bad"].state is StreamState.FAILED
        assert snapshots["bad"].error_code is CaptureErrorCode.STREAM_OPEN_FAILED
        assert snapshots["good"].state is StreamState.OPENING
        pool.reconcile([desired("good"), desired("bad")])
        assert backend.open_order == ["good"]
        assert len([call for call in resolver.calls if call[0] == "good"]) == 1
        assert len([call for call in resolver.calls if call[0] == "bad"]) == 1
    finally:
        pool.shutdown()


def test_failed_unpublished_close_retains_physical_capacity_and_shutdown_authority(
    tmp_path,
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    hardware_ids = ["failed"] + [f"replacement-{index}" for index in range(6)]
    for index, hardware_id in enumerate(hardware_ids):
        resolver.devices[hardware_id] = device(hardware_id, index=index)

    def fail_start_and_close(handle):
        handle.raise_on_start = OSError("start failed")
        handle.close_errors.extend(
            OSError("persistent close failure") for _ in range(32)
        )

    backend.handle_setups["failed"] = fail_start_and_close
    assert pool.reconcile([desired("failed")]).accepted
    failed_handle = backend.handles["failed"][0]
    assert not failed_handle.physically_closed
    assert pool.reconcile([]).accepted

    replacements = [desired(hardware_id) for hardware_id in hardware_ids[1:]]
    assert pool.reconcile(replacements).accepted
    all_handles = [
        handle
        for handles in backend.handles.values()
        for handle in handles
        if not handle.physically_closed
    ]
    assert len(all_handles) <= 6

    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.02
    shutdown = pool.shutdown()
    assert shutdown.state is PoolState.FAILED_CLOSED
    assert not failed_handle.physically_closed
    failed_handle.close_errors.clear()
    retried = pool.shutdown()
    assert retried.state is PoolState.CLOSED
    assert failed_handle.physically_closed


def test_failed_unpublished_close_retries_exact_resource_without_double_close(
    tmp_path,
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    hardware_ids = ["failed"] + [f"replacement-{index}" for index in range(6)]
    for index, hardware_id in enumerate(hardware_ids):
        resolver.devices[hardware_id] = device(hardware_id, index=index)

    def fail_start_and_close(handle):
        handle.raise_on_start = OSError("start failed")
        handle.close_errors.extend(
            OSError("retryable close failure") for _ in range(16)
        )

    backend.handle_setups["failed"] = fail_start_and_close
    try:
        assert pool.reconcile([desired("failed")]).accepted
        failed_handle = backend.handles["failed"][0]
        assert len(pool._unpublished_closes) == 1
        assert pool.reconcile([]).accepted

        replacements = [
            desired(hardware_id) for hardware_id in hardware_ids[1:]
        ]
        assert pool.reconcile(replacements).accepted
        assert sum(map(len, backend.handles.values())) == 6
        assert len(pool._unpublished_closes) == 1

        failed_handle.close_errors.clear()
        assert pool.reconcile(replacements).accepted
        assert failed_handle.physically_closed
        assert pool._unpublished_closes == {}
        assert len(pool.snapshot().published_hardware_ids) == 6
        close_calls = failed_handle.calls.count("close")

        assert pool.reconcile(replacements).accepted
        assert failed_handle.calls.count("close") == close_calls
        assert all(
            len(backend.handles[hardware_id]) == 1
            for hardware_id in hardware_ids[1:]
        )
    finally:
        pool.shutdown()


def test_superseded_open_executions_retain_physical_capacity_until_transfer(
    tmp_path,
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices["mic-a"] = device("mic-a")
    backend.open_return_releases["mic-a"] = threading.Event()
    workers = []

    try:
        for attempt_index in range(6):
            target = (
                (lambda: pool.reconcile([desired("mic-a")]))
                if attempt_index == 0
                else (lambda: pool.reconnect("mic-a"))
            )
            worker = threading.Thread(target=target)
            workers.append(worker)
            worker.start()
            wait_until(
                lambda expected=attempt_index + 1: (
                    len(backend.handles["mic-a"]) == expected
                )
            )
            assert sum(
                not handle.physically_closed
                for handle in backend.handles["mic-a"]
            ) <= 6

        seventh = threading.Thread(target=lambda: pool.reconnect("mic-a"))
        workers.append(seventh)
        seventh.start()
        time.sleep(0.05)
        assert len(backend.handles["mic-a"]) == 6
        assert sum(
            not handle.physically_closed
            for handle in backend.handles["mic-a"]
        ) == 6
    finally:
        backend.open_return_releases["mic-a"].set()
        for worker in workers:
            worker.join(2.0)

    assert all(not worker.is_alive() for worker in workers)
    wait_until(
        lambda: all(
            handle.physically_closed
            for handle in backend.handles["mic-a"]
        )
    )
    assert all(
        handle.calls.count("close") == 1
        for handle in backend.handles["mic-a"]
    )

    assert pool.reconcile([desired("mic-a")]).accepted
    assert len(backend.handles["mic-a"]) == 7
    assert pool.snapshot().published_hardware_ids == ("mic-a",)
    assert sum(
        not handle.physically_closed
        for handle in backend.handles["mic-a"]
    ) == 1
    pool.shutdown()
    assert all(
        handle.calls.count("close") == 1
        for handle in backend.handles["mic-a"]
    )


def test_stale_resolver_open_and_shutdown_results_never_publish(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices.update(
        {"slow-resolve": device("slow-resolve"), "slow-open": device("slow-open")}
    )
    resolver.releases["slow-resolve"] = threading.Event()
    thread = threading.Thread(
        target=lambda: pool.reconcile([desired("slow-resolve")])
    )
    thread.start()
    assert resolver.started["slow-resolve"].wait(1.0)
    assert pool.reconcile([]).accepted
    assert resolver.calls[0][3].is_set()
    resolver.releases["slow-resolve"].set()
    thread.join(1.0)
    assert "slow-resolve" not in pool.snapshot().published_hardware_ids
    assert backend.handles["slow-resolve"] == []

    backend.open_releases["slow-open"] = threading.Event()
    thread = threading.Thread(target=lambda: pool.reconcile([desired("slow-open")]))
    thread.start()
    assert backend.open_started["slow-open"].wait(1.0)
    shutdown = threading.Thread(target=pool.shutdown)
    shutdown.start()
    backend.open_releases["slow-open"].set()
    thread.join(1.0)
    shutdown.join(1.0)
    assert pool.snapshot().state is PoolState.CLOSED
    assert "slow-open" not in pool.snapshot().published_hardware_ids
    assert backend.handles["slow-open"][-1].physically_closed


def test_shutdown_failed_closed_while_resolver_execution_is_unsettled(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    pool._shutdown_control_timeout = 0.02
    resolver.devices["mic-a"] = device("mic-a")
    resolver.releases["mic-a"] = threading.Event()
    reconcile = threading.Thread(
        target=lambda: pool.reconcile([desired("mic-a")]),
        name="resident-audio-resolver-mic-a",
    )
    reconcile.start()
    assert resolver.started["mic-a"].wait(1.0)

    snapshot = pool.shutdown()
    try:
        assert snapshot.state is PoolState.FAILED_CLOSED
        assert reconcile.is_alive()
        assert "mic-a" in pool._opening_attempts
        assert backend.handles["mic-a"] == []
        assert any(
            resource.resource_kind == "resolver"
            and resource.hardware_id == "mic-a"
            and resource.thread_name == "resident-audio-resolver-mic-a"
            and resource.stream_generation is None
            and resource.run_id is None
            and resource.thread_alive
            for resource in snapshot.shutdown_resources
        )
    finally:
        resolver.releases["mic-a"].set()
        reconcile.join(2.0)
    assert pool.snapshot().state is PoolState.FAILED_CLOSED
    assert backend.handles["mic-a"] == []


def test_shutdown_failed_closed_until_blocked_open_returns_and_late_handle_closes(
    tmp_path,
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    pool._shutdown_control_timeout = 0.02
    resolver.devices["mic-a"] = device("mic-a")
    backend.open_releases["mic-a"] = threading.Event()
    reconcile = threading.Thread(
        target=lambda: pool.reconcile([desired("mic-a")])
    )
    reconcile.start()
    assert backend.open_started["mic-a"].wait(1.0)

    snapshot = pool.shutdown()
    try:
        assert snapshot.state is PoolState.FAILED_CLOSED
        assert reconcile.is_alive()
        assert "mic-a" in pool._opening_attempts
    finally:
        backend.open_releases["mic-a"].set()
        reconcile.join(2.0)
    assert pool.snapshot().state is PoolState.FAILED_CLOSED
    assert not backend.handles["mic-a"][0].physically_closed
    with pool._lock:
        late_stream = pool._shutdown_pending_streams[
            next(iter(pool._shutdown_pending_streams))
        ]
    assert (
        late_stream.tracked_cleanup_handle
        is backend.handles["mic-a"][0]
    )
    pool._shutdown_control_timeout = 1.0
    assert pool.shutdown().state is PoolState.CLOSED
    assert backend.handles["mic-a"][0].physically_closed
    assert backend.close_order.count("mic-a") == 1
    assert pool._shutdown_close_resources == {}


def test_concurrent_shutdown_callers_join_single_lifecycle_owner(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    handle, _ = make_ready(pool, resolver, backend)
    schedule_entered = threading.Event()
    schedule_continue = threading.Event()
    original_schedule = pool._schedule_shutdown_close

    def blocked_schedule(*args, **kwargs):
        schedule_entered.set()
        assert schedule_continue.wait(2.0)
        return original_schedule(*args, **kwargs)

    monkeypatch.setattr(pool, "_schedule_shutdown_close", blocked_schedule)
    results = []
    first = threading.Thread(target=lambda: results.append(pool.shutdown()))
    first.start()
    assert schedule_entered.wait(1.0)

    second = threading.Thread(target=lambda: results.append(pool.shutdown()))
    second.start()
    time.sleep(0.05)
    try:
        assert second.is_alive()
        assert not handle.physically_closed
        assert results == []
    finally:
        schedule_continue.set()
        first.join(2.0)
        second.join(2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert handle.physically_closed
    assert len(results) == 2
    assert all(result.state is PoolState.CLOSED for result in results)
    assert results[0] is results[1]


def test_shutdown_closed_before_result_store_still_joins_one_report(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    make_ready(pool, resolver, backend)
    owner_finished = threading.Event()
    owner_publish = threading.Event()
    original_run_shutdown = pool._run_shutdown

    def blocked_result_store(deadline):
        report = original_run_shutdown(deadline)
        owner_finished.set()
        assert owner_publish.wait(2.0)
        return report

    monkeypatch.setattr(pool, "_run_shutdown", blocked_result_store)
    reports = []
    first = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    first.start()
    assert owner_finished.wait(1.0)
    assert pool._state is PoolState.CLOSED
    assert pool._shutdown_result is None

    second = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    second.start()
    time.sleep(0.05)
    try:
        assert second.is_alive()
        assert reports == []
    finally:
        owner_publish.set()
        first.join(2.0)
        second.join(2.0)

    repeated = pool.shutdown()
    assert len(reports) == 2
    assert reports[0] is reports[1] is repeated


def test_shutdown_owner_exception_publishes_shared_failed_report(
    tmp_path, monkeypatch
):
    pool, *_ = make_pool(tmp_path)
    owner_entered = threading.Event()
    owner_raise = threading.Event()

    def injected_failure(_deadline):
        owner_entered.set()
        assert owner_raise.wait(2.0)
        raise RuntimeError("injected shutdown failure")

    monkeypatch.setattr(pool, "_run_shutdown", injected_failure)
    reports = []
    errors = []

    def shutdown_call():
        try:
            reports.append(pool.shutdown())
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=shutdown_call)
    first.start()
    assert owner_entered.wait(1.0)
    second = threading.Thread(target=shutdown_call)
    second.start()
    time.sleep(0.05)
    owner_raise.set()
    first.join(2.0)
    second.join(2.0)

    repeated = pool.shutdown()
    assert errors == []
    assert len(reports) == 2
    assert reports[0] is reports[1] is repeated
    assert repeated.state is PoolState.FAILED_CLOSED


def test_failed_closed_retry_is_management_only_and_single_flight(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    original_close = stream.close
    retry_entered = threading.Event()
    retry_continue = threading.Event()
    fail_close = True
    close_calls = 0

    def controlled_close(*args, **kwargs):
        nonlocal close_calls
        close_calls += 1
        if fail_close:
            return False
        retry_entered.set()
        assert retry_continue.wait(2.0)
        return original_close(*args, **kwargs)

    monkeypatch.setattr(stream, "close", controlled_close)
    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.02
    first = pool.shutdown()
    assert first.state is PoolState.FAILED_CLOSED
    assert close_calls == 2
    assert backend.force_terminate_calls == 1
    assert not handle.physically_closed

    assert not pool.reconcile([desired("mic-a")]).accepted
    assert pool.reserve("late-run").error_code is CaptureErrorCode.POOL_SHUTTING_DOWN
    assert pool.commit(
        "late-run", token, request(token, "late-run")
    ).error_code is CaptureErrorCode.POOL_SHUTTING_DOWN

    fail_close = False
    pool._shutdown_control_timeout = 1.0
    reports = []
    second = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    second.start()
    assert retry_entered.wait(1.0)
    third = threading.Thread(target=lambda: reports.append(pool.shutdown()))
    third.start()
    time.sleep(0.05)
    try:
        assert third.is_alive()
        assert close_calls == 3
    finally:
        retry_continue.set()
        second.join(2.0)
        third.join(2.0)

    repeated = pool.shutdown()
    assert handle.physically_closed
    assert close_calls == 3
    assert len(reports) == 2
    assert reports[0] is reports[1] is repeated
    assert repeated.state is PoolState.CLOSED


def test_failed_closed_retry_closes_exact_managed_removal(
    tmp_path, monkeypatch
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    handle, _ = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    original_close = stream.close
    permit_close = False
    close_calls = 0

    def controlled_close(*args, **kwargs):
        nonlocal close_calls
        close_calls += 1
        if not permit_close:
            return False
        return original_close(*args, **kwargs)

    monkeypatch.setattr(stream, "close", controlled_close)
    assert pool.reconcile([]).accepted
    assert len(pool._closing_streams) == 1
    pool._shutdown_control_timeout = 0.02
    pool._shutdown_force_grace = 0.02
    assert pool.shutdown().state is PoolState.FAILED_CLOSED
    assert close_calls == 3
    assert backend.force_terminate_calls == 1
    assert not handle.physically_closed

    permit_close = True
    retried = pool.shutdown()
    assert retried.state is PoolState.CLOSED
    assert handle.physically_closed
    assert close_calls == 4
    assert pool._closing_streams == {}
    assert pool.shutdown() is retried


def test_generation_is_persistent_across_remove_readd_reconnect_and_failures(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices["mic-a"] = device("mic-a")
    try:
        pool.reconcile([desired("mic-a")])
        first = pool.snapshot().stream_for("mic-a").stream_generation
        pool.reconcile([])
        pool.reconcile([desired("mic-a")])
        second = pool.snapshot().stream_for("mic-a").stream_generation
        backend.failures["mic-a"] = OSError("failed generation")
        pool.reconnect("mic-a")
        failed = pool.snapshot().stream_for("mic-a").stream_generation
        del backend.failures["mic-a"]
        pool.reconnect("mic-a")
        fourth = pool.snapshot().stream_for("mic-a").stream_generation
        assert (first, second, failed, fourth) == (1, 2, 3, 4)

        other, *_ = make_pool(tmp_path / "other")
        try:
            assert other.pool_epoch != pool.pool_epoch
        finally:
            other.shutdown()
    finally:
        pool.shutdown()


def test_resolver_failure_does_not_consume_stream_generation(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices["mic-a"] = device("mic-a")
    resolver.failures["mic-a"] = OSError("enumeration failed")
    try:
        assert pool.reconcile([desired("mic-a")]).accepted
        assert pool.snapshot().stream_for("mic-a").state is StreamState.FAILED
        del resolver.failures["mic-a"]
        assert pool.reconnect("mic-a").accepted
        assert pool.snapshot().stream_for("mic-a").stream_generation == 1
        assert len(backend.handles["mic-a"]) == 1
    finally:
        pool.shutdown()


def test_superseding_open_attempt_closes_old_generation_and_publishes_only_new(
    tmp_path,
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices["mic-a"] = device("mic-a")
    backend.open_releases["mic-a"] = threading.Event()
    first = threading.Thread(target=lambda: pool.reconcile([desired("mic-a")]))
    first.start()
    assert backend.open_started["mic-a"].wait(1.0)
    second = threading.Thread(target=lambda: pool.reconnect("mic-a"))
    second.start()
    wait_until(lambda: len(resolver.calls) == 2)
    backend.open_releases["mic-a"].set()
    first.join(2.0)
    second.join(2.0)
    try:
        snapshot = pool.snapshot().stream_for("mic-a")
        assert snapshot.stream_generation == 2
        assert len(backend.handles["mic-a"]) == 2
        assert sum(handle.physically_closed for handle in backend.handles["mic-a"]) == 1
    finally:
        pool.shutdown()


def test_remove_readd_supersedes_blocked_resolution_with_new_generation(tmp_path):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    resolver.devices["mic-a"] = device("mic-a")
    resolver.releases["mic-a"] = threading.Event()
    first = threading.Thread(target=lambda: pool.reconcile([desired("mic-a")]))
    first.start()
    assert resolver.started["mic-a"].wait(1.0)
    assert pool.reconcile([]).accepted
    second = threading.Thread(target=lambda: pool.reconcile([desired("mic-a")]))
    second.start()
    wait_until(lambda: len(resolver.calls) == 2)
    resolver.releases["mic-a"].set()
    first.join(2.0)
    second.join(2.0)
    try:
        snapshot = pool.snapshot().stream_for("mic-a")
        assert snapshot.stream_generation == 1
        assert len(backend.handles["mic-a"]) == 1
    finally:
        pool.shutdown()


def test_six_lease_capacity_duplicate_ids_and_terminal_idempotency(tmp_path):
    pool, *_ = make_pool(tmp_path)
    try:
        outcomes = [pool.reserve(f"run-{index}") for index in range(6)]
        assert all(outcome.state is LeaseState.RESERVED for outcome in outcomes)
        seventh = pool.reserve("run-6")
        assert seventh.state is LeaseState.FAILED
        assert seventh.error_code is CaptureErrorCode.CAPTURE_CAPACITY_FULL
        assert pool.reserve("run-6") is seventh
        canceled = pool.cancel("run-0", "operator")
        assert canceled.state is LeaseState.CANCELED
        assert pool.cancel("run-0", "again") is canceled
        assert pool.reserve("run-0") is canceled
        assert pool.snapshot().active_lease_count == 5
        assert pool.reserve("run-7").state is LeaseState.RESERVED
    finally:
        pool.shutdown()


def test_six_active_or_settling_leases_block_seventh_until_final_terminal_cas(
    tmp_path,
    monkeypatch,
):
    pool, resolver, backend, _store, _clock = make_pool(tmp_path)
    _handle, token = make_ready(pool, resolver, backend)
    try:
        for index in range(5):
            assert pool.reserve(f"held-{index}").state is LeaseState.RESERVED
        assert pool.reserve("settling").state is LeaseState.RESERVED
        assert (
            pool.commit(
                "settling",
                token,
                request(token, "settling", duration=1.0),
            ).state
            is LeaseState.COMMITTED
        )

        adapter = pool._settlement_adapter
        original_terminal_cas = adapter.terminal_cas
        terminal_entered = threading.Event()

        def pause_terminal_cas(claim, progress, summary):
            terminal_entered.set()
            return None

        monkeypatch.setattr(adapter, "terminal_cas", pause_terminal_cas)
        canceled = pool.cancel("settling", "operator")
        assert canceled.state is LeaseState.COMMITTED
        assert terminal_entered.is_set()
        assert pool.lease_snapshot("settling").settled is False
        assert pool.snapshot().active_lease_count == 6

        blocked = pool.reserve("blocked-seventh")
        assert blocked.state is LeaseState.FAILED
        assert blocked.error_code is CaptureErrorCode.CAPTURE_CAPACITY_FULL

        monkeypatch.setattr(adapter, "terminal_cas", original_terminal_cas)
        pool.retry_settlements()
        assert pool.lease_snapshot("settling").state is LeaseState.CANCELED
        assert pool.lease_snapshot("settling").settled is True
        assert pool.snapshot().active_lease_count == 5
        assert pool.reserve("admitted-seventh").state is LeaseState.RESERVED
    finally:
        pool.shutdown()


def test_concurrent_reserve_never_exceeds_six(tmp_path):
    pool, *_ = make_pool(tmp_path)
    barrier = threading.Barrier(17)
    results = []
    result_lock = threading.Lock()

    def reserve(index):
        barrier.wait()
        outcome = pool.reserve(f"run-{index}")
        with result_lock:
            results.append(outcome)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(1.0)
    try:
        assert sum(item.state is LeaseState.RESERVED for item in results) == 6
        assert pool.snapshot().active_lease_count == 6
    finally:
        pool.shutdown()


def test_invalid_request_fails_before_committing_and_blocks_reconcile_only_while_active(
    tmp_path,
):
    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    try:
        reserved = pool.reserve("invalid")
        assert reserved.state is LeaseState.RESERVED
        refused = pool.reconcile([])
        assert not refused.accepted
        assert refused.error_code is CaptureErrorCode.INVALID_CAPTURE_REQUEST

        invalid = request(token, "invalid", duration=-1.0)
        outcome = pool.commit("invalid", token, invalid)
        assert outcome.state is LeaseState.FAILED
        assert outcome.error_code is CaptureErrorCode.INVALID_CAPTURE_REQUEST
        assert pool.commit("invalid", token, invalid) is outcome
        assert pool.cancel("invalid", "late") is outcome
        assert pool.reconcile([]).accepted
    finally:
        pool.shutdown()


def test_future_trigger_request_token_and_generation_validate_before_prepare(tmp_path):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    try:
        pool.reserve("future")
        future = pool.commit(
            "future",
            token,
            request(token, "future", trigger=clock() + 1.0),
        )
        assert future.state is LeaseState.FAILED
        assert future.error_code is CaptureErrorCode.INVALID_CAPTURE_REQUEST

        stale = replace(token, stream_generation=token.stream_generation + 1)
        pool.reserve("stale")
        stale_outcome = pool.commit("stale", stale, request(stale, "stale"))
        assert stale_outcome.state is LeaseState.FAILED
        assert stale_outcome.error_code is CaptureErrorCode.STREAM_GENERATION_CHANGED

        pool.reserve("mismatch")
        mismatch = pool.commit(
            "mismatch", token, request(stale, "mismatch")
        )
        assert mismatch.state is LeaseState.FAILED
        assert mismatch.error_code is CaptureErrorCode.INVALID_CAPTURE_REQUEST
        assert store.reserved_bytes == 0
        assert pool.snapshot().committing_leases == 0
    finally:
        pool.shutdown()


def test_two_phase_commit_success_release_claim_and_quota_lifetime(tmp_path):
    pool, resolver, backend, store, clock = make_pool(tmp_path, quota_bytes=20)
    claimed = []

    def completion(completion):
        claim = completion.claim("save-worker")
        assert claim is not None
        assert store.accept_transfer(claim)
        claimed.append(claim)

    pool.set_completion_handler(completion)
    handle, token = make_ready(pool, resolver, backend)
    try:
        assert pool.reserve("run-a").state is LeaseState.RESERVED
        committed = pool.commit("run-a", token, request(token, "run-a"))
        assert committed.state is LeaseState.COMMITTED
        clock.advance(0.05)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=10.05,
            adc_time=10.04,
            current_time=10.05,
        )
        pool.synchronize_stream("mic-a", timeout=1.0)
        released = wait_until(
            lambda: (
                outcome
                if (outcome := pool.lease_outcome("run-a")).state
                is LeaseState.RELEASED
                else None
            )
        )
        assert released.artifact_id
        assert pool.reserve("run-b").state is LeaseState.RESERVED
        storage_failure = pool.commit("run-b", token, request(token, "run-b"))
        assert storage_failure.state is LeaseState.FAILED
        assert storage_failure.error_code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY
        assert store.reserved_bytes == 20
        assert store.release_transferred(claimed[0], "saved")
        assert store.reserved_bytes == 0
        assert store.release_transferred(claimed[0], "again")
    finally:
        pool.shutdown()


def test_delete_pending_artifact_retains_quota_until_retry_succeeds(tmp_path):
    fail_delete = [True]

    def remove_file(path):
        if fail_delete[0]:
            raise OSError("busy")
        path.unlink()

    resolver = PoolResolver()
    backend = MultiBackend()
    clock = ManualClock(10.0)
    store = CaptureArtifactStore(
        tmp_path,
        quota_bytes=20,
        free_space_bytes=lambda _path: 10_000_000,
        minimum_free_bytes=0,
        remove_file=remove_file,
    )
    pool = ResidentAudioPool(
        resolver=resolver,
        backend=backend,
        artifact_store=store,
        monotonic_clock=clock,
    )
    claims = []

    def completion(completion):
        claim = completion.claim("save-worker")
        assert store.accept_transfer(claim)
        claims.append(claim)

    pool.set_completion_handler(completion)
    handle, token = make_ready(pool, resolver, backend)
    try:
        pool.reserve("run-a")
        pool.commit("run-a", token, request(token, "run-a"))
        clock.advance(0.05)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        pool.synchronize_stream("mic-a", timeout=1.0)
        outcome = wait_until(
            lambda: (
                value
                if (value := pool.lease_outcome("run-a")).state
                is LeaseState.RELEASED
                else None
            )
        )
        assert store.release_transferred(claims[0], "saved")
        assert store.state(outcome.artifact_id) is ArtifactState.DELETE_PENDING
        assert store.reserved_bytes == 20
        pool.reserve("run-b")
        rejected = pool.commit("run-b", token, request(token, "run-b"))
        assert rejected.error_code is CaptureErrorCode.CAPTURE_STORAGE_CAPACITY

        fail_delete[0] = False
        report = store.retry_delete_pending(limit=1)
        assert len(report.deleted_paths) == 1
        assert store.reserved_bytes == 0
        assert store.state(outcome.artifact_id) is ArtifactState.RELEASED
    finally:
        pool.shutdown()


def test_release_identity_guards_and_cancel_cleans_committed_owner(tmp_path):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    try:
        pool.reserve("run-a")
        committed = pool.commit(
            "run-a", token, request(token, "run-a", duration=0.5)
        )
        assert committed.state is LeaseState.COMMITTED
        assert not hasattr(committed, "owner_capability")
        assert not pool.reconcile([]).accepted
        assert pool.release("run-a", "wrong", "wrong") is committed
        lease = pool.lease_snapshot("run-a")
        assert not hasattr(lease, "owner_capability")
        assert pool.release("run-a", "guessed-private-authority", "wrong") is committed

        canceled = pool.cancel("run-a", "operator")
        assert canceled.state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert pool.snapshot().stream_for("mic-a").owner_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_committed_state_and_lock_free_owner_token_open_at_one_pool_cas(
    tmp_path, monkeypatch
):
    from app.service.analysis.resident_audio.owner_types import CommitToken

    pool, resolver, backend, _, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    token_open_entered = threading.Event()
    token_open_continue = threading.Event()
    observed_tokens = []
    original_open = CommitToken.open

    def blocked_open(commit_token):
        observed_tokens.append(commit_token)
        token_open_entered.set()
        assert token_open_continue.wait(2.0)
        return original_open(commit_token)

    monkeypatch.setattr(CommitToken, "open", blocked_open)
    outcomes = []
    commit_worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    commit_worker.start()
    assert token_open_entered.wait(1.0)

    observed_outcomes = []
    observer = threading.Thread(
        target=lambda: observed_outcomes.append(pool.lease_outcome("run-a"))
    )
    observer.start()
    time.sleep(0.02)
    assert observer.is_alive()
    token_open_continue.set()
    commit_worker.join(2.0)
    observer.join(2.0)
    try:
        assert outcomes[0].state is LeaseState.COMMITTED
        assert observed_outcomes[0].state is LeaseState.COMMITTED
        assert observed_tokens[0].is_open
    finally:
        pool.shutdown()


def test_cancel_during_prepare_rolls_back_before_terminal_publication(tmp_path, monkeypatch):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    entered = threading.Event()
    release = threading.Event()
    original = stream.prepare_owner

    def blocked_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(2.0)
        return result

    monkeypatch.setattr(stream, "prepare_owner", blocked_prepare)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert entered.wait(1.0)
    assert not pool.reconcile([]).accepted
    cancel_result = pool.cancel("run-a", "race")
    assert cancel_result.state is LeaseState.COMMITTING
    release.set()
    worker.join(2.0)
    try:
        assert outcomes[0].state is LeaseState.CANCELED
        assert pool.snapshot().stream_for("mic-a").owner_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_dormant_rollback_remove_failure_retains_capacity_until_retry(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    prepared = threading.Event()
    continue_commit = threading.Event()
    original_prepare = stream.prepare_owner
    original_remove = stream.remove_owner
    remove_calls = 0

    def blocked_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        prepared.set()
        assert continue_commit.wait(2.0)
        return result

    def fail_once_remove(*args, **kwargs):
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            return False
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(stream, "prepare_owner", blocked_prepare)
    monkeypatch.setattr(stream, "remove_owner", fail_once_remove)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert prepared.wait(1.0)
    assert pool.cancel("run-a", "cancel dormant").state is LeaseState.COMMITTING
    continue_commit.set()
    worker.join(2.0)
    try:
        assert outcomes[0].state is LeaseState.COMMITTING
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1
        assert store.reserved_bytes > 0

        retried = pool.cancel("run-a", "retry dormant cleanup")
        assert retried.state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_exact_detach_receipt_settles_one_lease_without_touching_another(
    tmp_path,
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    for run_id in ("run-a", "run-b"):
        pool.reserve(run_id)
        assert pool.commit(
            run_id, token, request(token, run_id, duration=0.5)
        ).state is LeaseState.COMMITTED
    stream = pool.stream_for_testing("mic-a")
    owner_a = pool._leases["run-a"].owner
    identity_a = pool._leases["run-a"].identity

    assert stream.submit_owner_control(
        OwnerControlEvent(
            kind=OwnerControlKind.DETACH,
            owner=owner_a,
            run_id=identity_a.run_id,
            commit_attempt_id=identity_a.commit_attempt_id,
            owner_capability=identity_a.owner_capability,
            pool_epoch=identity_a.pool_epoch,
            stream_generation=identity_a.stream_generation,
        )
    )
    assert stream.synchronize(timeout=1.0)
    assert pool.snapshot().stream_for("mic-a").owner_count == 1

    try:
        canceled = pool.cancel("run-a", "already exactly detached")
        assert canceled.state is LeaseState.CANCELED
        assert pool.lease_snapshot("run-a").settled
        assert pool.lease_outcome("run-b").state is LeaseState.COMMITTED
        assert pool.snapshot().active_lease_count == 1
        assert pool.snapshot().stream_for("mic-a").owner_count == 1
        assert store.reserved_bytes > 0

        assert pool.cancel("run-b", "cleanup remaining").state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


@pytest.mark.parametrize("prepare_final", ["true", "false", "raise"])
def test_indeterminate_prepare_retains_cleanup_until_exact_tombstone(
    tmp_path, monkeypatch, prepare_final
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    prepare_claimed = threading.Event()
    prepare_continue = threading.Event()
    original_control_prepare = stream._prepare
    original_prepare_owner = stream.prepare_owner

    def late_control_prepare(*args, **kwargs):
        prepare_claimed.set()
        assert prepare_continue.wait(2.0)
        if prepare_final == "true":
            return original_control_prepare(*args, **kwargs)
        if prepare_final == "false":
            return False
        raise RuntimeError("injected late prepare failure")

    def quickly_indeterminate_prepare(owner, identity, *, timeout=None):
        return original_prepare_owner(owner, identity, timeout=0.01)

    monkeypatch.setattr(stream, "_prepare", late_control_prepare)
    monkeypatch.setattr(
        stream, "prepare_owner", quickly_indeterminate_prepare
    )
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert prepare_claimed.wait(1.0)
    worker.join(1.5)
    try:
        assert not worker.is_alive()
        assert outcomes[0].state is LeaseState.COMMITTING
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1
        assert store.reserved_bytes > 0

        prepare_continue.set()
        assert stream.synchronize(timeout=1.0)
        assert pool.snapshot().stream_for("mic-a").owner_count == 0

        retried = pool.cancel("run-a", "retry indeterminate prepare")
        assert retried.state is LeaseState.FAILED
        assert pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 0
        assert pool.snapshot().stream_for("mic-a").owner_count == 0
        assert store.reserved_bytes == 0
    finally:
        prepare_continue.set()
        worker.join(2.0)
        pool.shutdown()


@pytest.mark.parametrize(
    "failure_mode",
    ["false", "exception", "mismatch_receipt", "indeterminate_receipt"],
)
def test_committed_cancel_remove_failure_retains_capacity_until_retry(
    tmp_path, monkeypatch, failure_mode
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    stream = pool.stream_for_testing("mic-a")
    original_remove = stream.remove_owner
    remove_calls = 0

    def fail_once_remove(*args, **kwargs):
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            if failure_mode == "exception":
                raise RuntimeError("remove acknowledgement lost")
            if failure_mode.endswith("_receipt"):
                outcome = (
                    OwnerRemovalOutcome.MISMATCH
                    if failure_mode == "mismatch_receipt"
                    else OwnerRemovalOutcome.INDETERMINATE
                )
                return OwnerRemovalReceipt(args[0], outcome)
            return False
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(stream, "remove_owner", fail_once_remove)
    try:
        first = pool.cancel("run-a", "cancel active")
        assert first.state is LeaseState.COMMITTED
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1
        assert store.reserved_bytes > 0

        retried = pool.cancel("run-a", "retry active cleanup")
        assert retried.state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


@pytest.mark.parametrize(
    "outcome",
    [OwnerRemovalOutcome.MISMATCH, OwnerRemovalOutcome.INDETERMINATE],
)
def test_indeterminate_or_mismatch_detach_keeps_lease_nonterminal(
    tmp_path, monkeypatch, outcome
):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    completions = []
    pool.set_completion_handler(completions.append)
    handle, token = make_ready(pool, resolver, backend)
    pool.reserve("run-completion-finality")
    assert pool.commit(
        "run-completion-finality",
        token,
        request(token, "run-completion-finality"),
    ).state is LeaseState.COMMITTED
    stream = pool.stream_for_testing("mic-a")
    original_remove = stream.remove_owner
    removal_attempted = threading.Event()

    def nonconfirming_remove(instance_id, **_kwargs):
        removal_attempted.set()
        return OwnerRemovalReceipt(instance_id, outcome)

    monkeypatch.setattr(stream, "remove_owner", nonconfirming_remove)
    clock.advance(0.05)
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert removal_attempted.wait(1.0)
    try:
        lease = pool.lease_snapshot("run-completion-finality")
        assert lease.state is LeaseState.COMMITTED
        assert lease.settled is False
        assert pool.snapshot().active_lease_count == 1
        assert completions == []
        assert store.reserved_bytes > 0

        monkeypatch.setattr(stream, "remove_owner", original_remove)
        pool.retry_settlements()
        assert (
            pool.lease_outcome("run-completion-finality").state
            is LeaseState.FAILED
        )
        assert (
            pool.lease_outcome(
                "run-completion-finality"
            ).error_code
            is CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
        )
        assert len(completions) == 1
    finally:
        pool.shutdown()


def test_committed_cancel_join_failure_retains_capacity_until_retry(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    owner = pool._leases["run-a"].owner
    original_join = owner.join
    join_calls = 0

    def fail_once_join(timeout=None):
        nonlocal join_calls
        join_calls += 1
        if join_calls == 1:
            return False
        return original_join(timeout)

    monkeypatch.setattr(owner, "join", fail_once_join)
    try:
        first = pool.cancel("run-a", "cancel with blocked owner")
        assert first.state is LeaseState.COMMITTED
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1

        retried = pool.cancel("run-a", "retry owner join")
        assert retried.state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_committed_cancel_artifact_cleanup_failure_retains_quota_until_retry(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    original_discard = store.discard_partial
    reject_discard = True

    def controlled_discard(artifact_id):
        if reject_discard:
            return False
        return original_discard(artifact_id)

    monkeypatch.setattr(store, "discard_partial", controlled_discard)
    try:
        first = pool.cancel("run-a", "cancel with staging failure")
        assert first.state is LeaseState.COMMITTED
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1
        assert store.reserved_bytes > 0

        reject_discard = False
        retried = pool.cancel("run-a", "retry staging cleanup")
        assert retried.state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_committed_cancel_blocked_artifact_thread_retains_capacity_until_retry(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a", duration=0.5)
    ).state is LeaseState.COMMITTED
    owner = pool._leases["run-a"].owner
    original_join = owner.join
    original_discard = store.discard_partial
    discard_entered = threading.Event()
    discard_continue = threading.Event()
    join_calls = 0

    def blocked_discard(artifact_id):
        discard_entered.set()
        assert discard_continue.wait(2.0)
        return original_discard(artifact_id)

    def fail_once_join(timeout=None):
        nonlocal join_calls
        join_calls += 1
        if join_calls == 1:
            return False
        return original_join(timeout)

    monkeypatch.setattr(store, "discard_partial", blocked_discard)
    monkeypatch.setattr(owner, "join", fail_once_join)
    try:
        first = pool.cancel("run-a", "cancel with blocked staging")
        assert not discard_entered.wait(0.05)
        assert first.state is LeaseState.COMMITTED
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1
        assert store.reserved_bytes > 0

        retry_outcomes = []
        retry = threading.Thread(
            target=lambda: retry_outcomes.append(
                pool.cancel("run-a", "retry blocked staging")
            )
        )
        retry.start()
        assert discard_entered.wait(1.0)
        assert retry.is_alive()
        assert not pool.lease_snapshot("run-a").settled
        assert pool.snapshot().active_lease_count == 1
        assert store.reserved_bytes > 0
        discard_continue.set()
        retry.join(2.0)
        assert not retry.is_alive()
        assert retry_outcomes[0].state is LeaseState.CANCELED
        assert pool.snapshot().active_lease_count == 0
        assert store.reserved_bytes == 0
    finally:
        discard_continue.set()
        pool.shutdown()


def test_cancel_after_activation_ack_removes_owner_before_terminal(tmp_path, monkeypatch):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    activated = threading.Event()
    return_from_activate = threading.Event()
    original = stream.activate_owner

    def activated_then_blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        activated.set()
        assert return_from_activate.wait(2.0)
        return result

    monkeypatch.setattr(stream, "activate_owner", activated_then_blocked)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert activated.wait(1.0)
    canceled = pool.cancel("run-a", "activation race")
    assert canceled.state is LeaseState.CANCELED
    assert pool.snapshot().stream_for("mic-a").owner_count == 0
    return_from_activate.set()
    worker.join(2.0)
    try:
        assert outcomes[0] is canceled
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_generation_replacement_during_activation_rolls_back_exact_owner(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    entered = threading.Event()
    release = threading.Event()
    original = stream.activate_owner

    def blocked_activate(*args, **kwargs):
        entered.set()
        assert release.wait(2.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(stream, "activate_owner", blocked_activate)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert entered.wait(1.0)
    stream.report_backend_failure(OSError("disconnect"), timeout=1.0)
    release.set()
    worker.join(2.0)
    try:
        assert outcomes[0].state is LeaseState.FAILED
        assert (
            outcomes[0].error_code
            is CaptureErrorCode.STREAM_OPEN_FAILED
        )
        assert pool.snapshot().stream_for("mic-a").owner_count == 0
        assert store.reserved_bytes == 0
    finally:
        pool.shutdown()


def test_shutdown_during_commit_waits_for_exact_rollback(tmp_path, monkeypatch):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    entered = threading.Event()
    release = threading.Event()
    original = stream.prepare_owner

    def blocked_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(2.0)
        return result

    monkeypatch.setattr(stream, "prepare_owner", blocked_prepare)
    commit_outcomes = []
    commit_thread = threading.Thread(
        target=lambda: commit_outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    commit_thread.start()
    assert entered.wait(1.0)
    close_thread = threading.Thread(target=pool.shutdown)
    close_thread.start()
    assert close_thread.is_alive()
    release.set()
    commit_thread.join(2.0)
    close_thread.join(2.0)
    assert commit_outcomes[0].state is LeaseState.CANCELED
    assert pool.snapshot().state is PoolState.CLOSED
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0


def test_shutdown_before_artifact_prepare_rejects_late_allocation(tmp_path, monkeypatch):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    allocation_entered = threading.Event()
    allocation_continue = threading.Event()
    original = store.allocate_writer

    def blocked_allocation(**kwargs):
        allocation_entered.set()
        assert allocation_continue.wait(2.0)
        return original(**kwargs)

    monkeypatch.setattr(store, "allocate_writer", blocked_allocation)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert allocation_entered.wait(1.0)
    shutdown = threading.Thread(target=pool.shutdown)
    shutdown.start()
    assert shutdown.is_alive()
    allocation_continue.set()
    worker.join(2.0)
    shutdown.join(2.0)
    assert outcomes[0].state is LeaseState.CANCELED
    assert pool.snapshot().state is PoolState.CLOSED
    assert store.reserved_bytes == 0


def test_blocked_commit_resource_reports_exact_thread_and_identity(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("blocked-commit-identity")
    allocation_entered = threading.Event()
    allocation_continue = threading.Event()
    original = store.allocate_writer

    def blocked_allocation(**kwargs):
        allocation_entered.set()
        allocation_continue.wait(1.0)
        return original(**kwargs)

    monkeypatch.setattr(store, "allocate_writer", blocked_allocation)
    worker = threading.Thread(
        target=lambda: pool.commit(
            "blocked-commit-identity",
            token,
            request(token, "blocked-commit-identity", duration=0.5),
        ),
        name="resident-audio-commit-blocked-commit-identity",
    )
    worker.start()
    assert allocation_entered.wait(1.0)
    pool._shutdown_control_timeout = 0.03
    pool._shutdown_force_grace = 0.03

    snapshot = pool.shutdown()
    try:
        assert snapshot.state is PoolState.FAILED_CLOSED
        resource = next(
            item
            for item in snapshot.shutdown_resources
            if item.resource_kind == "commit"
        )
        assert resource.thread_name == (
            "resident-audio-commit-blocked-commit-identity"
        )
        assert resource.run_id == "blocked-commit-identity"
        assert resource.hardware_id == "mic-a"
        assert resource.stream_generation == token.stream_generation
        assert resource.thread_alive
    finally:
        allocation_continue.set()
        worker.join(2.0)


@pytest.mark.parametrize(
    "release_within_grace",
    (True, False),
    ids=("commit-finishes-in-grace", "commit-outlives-grace"),
)
def test_forced_grace_joins_committing_worker_until_single_deadline(
    tmp_path, monkeypatch, release_within_grace
):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("forced-grace-commit")
    stream = pool.stream_for_testing("mic-a")
    allocation_entered = threading.Event()
    allocation_continue = threading.Event()
    close_continue = threading.Event()
    retry_entered = threading.Event()
    retry_continue = threading.Event()
    terminal_calls = []
    original_allocate = store.allocate_writer
    original_close = stream.close
    original_retry = pool._retry_failed_shutdown
    original_terminal = pool._settlement_adapter.terminal_cas

    def blocked_allocation(**kwargs):
        allocation_entered.set()
        allocation_continue.wait(1.0)
        return original_allocate(**kwargs)

    def blocked_close(*args, **kwargs):
        if not close_continue.wait(1.0):
            return False
        return original_close(*args, **kwargs)

    def force_terminate():
        backend.force_terminate_calls += 1
        close_continue.set()

    def observed_retry(deadline):
        retry_entered.set()
        retry_continue.wait(1.0)
        return original_retry(deadline)

    def terminal_cas(*args, **kwargs):
        terminal_calls.append(args[0].lease_id)
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(store, "allocate_writer", blocked_allocation)
    monkeypatch.setattr(stream, "close", blocked_close)
    monkeypatch.setattr(backend, "force_terminate", force_terminate)
    monkeypatch.setattr(pool, "_retry_failed_shutdown", observed_retry)
    monkeypatch.setattr(
        pool._settlement_adapter, "terminal_cas", terminal_cas
    )
    pool._shutdown_control_timeout = 0.03
    pool._shutdown_force_grace = 0.2
    commit = threading.Thread(
        target=lambda: pool.commit(
            "forced-grace-commit",
            token,
            request(token, "forced-grace-commit", duration=0.5),
        ),
        name="resident-audio-commit-forced-grace",
    )
    commit.start()
    assert allocation_entered.wait(1.0)
    reports = []
    shutdown = threading.Thread(
        target=lambda: reports.append(pool.shutdown()),
        name="resident-audio-forced-grace-shutdown",
    )
    shutdown.start()
    assert retry_entered.wait(1.0)
    assert backend.force_terminate_calls == 1
    retry_continue.set()

    try:
        if release_within_grace:
            threading.Event().wait(0.05)
            allocation_continue.set()
        shutdown.join(1.0)
        assert not shutdown.is_alive()
        assert backend.force_terminate_calls == 1
        if release_within_grace:
            assert reports[0].state is PoolState.CLOSED
            assert not commit.is_alive()
            assert terminal_calls == ["forced-grace-commit"]
        else:
            assert reports[0].state is PoolState.FAILED_CLOSED
            resource = next(
                item
                for item in reports[0].shutdown_resources
                if item.resource_kind == "commit"
            )
            assert resource.thread_name == (
                "resident-audio-commit-forced-grace"
            )
            assert resource.run_id == "forced-grace-commit"
            assert resource.hardware_id == "mic-a"
            assert resource.stream_generation == token.stream_generation
            assert resource.thread_alive
            assert commit.is_alive()
            assert terminal_calls == []
    finally:
        retry_continue.set()
        allocation_continue.set()
        close_continue.set()
        commit.join(2.0)
        shutdown.join(2.0)


def test_shutdown_after_activation_ack_cleans_before_terminal(tmp_path, monkeypatch):
    pool, resolver, backend, store, _ = make_pool(tmp_path)
    _, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    stream = pool.stream_for_testing("mic-a")
    activation_acked = threading.Event()
    activation_return = threading.Event()
    original = stream.activate_owner

    def acked_then_blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        activation_acked.set()
        assert activation_return.wait(2.0)
        return result

    monkeypatch.setattr(stream, "activate_owner", acked_then_blocked)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            pool.commit(
                "run-a", token, request(token, "run-a", duration=0.5)
            )
        )
    )
    worker.start()
    assert activation_acked.wait(1.0)
    shutdown = threading.Thread(target=pool.shutdown)
    shutdown.start()
    wait_until(lambda: pool.lease_outcome("run-a").state is LeaseState.CANCELED)
    activation_return.set()
    worker.join(2.0)
    shutdown.join(2.0)
    assert outcomes[0].state is LeaseState.CANCELED
    assert pool.snapshot().state is PoolState.CLOSED
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0


def test_success_linearized_before_shutdown_waits_for_handler_decision(tmp_path):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    handler_entered = threading.Event()
    handler_decide = threading.Event()

    def completion(completion):
        handler_entered.set()
        assert handler_decide.wait(2.0)
        assert completion.decline("shutdown")

    pool.set_completion_handler(completion)
    handle, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit("run-a", token, request(token, "run-a")).state is LeaseState.COMMITTED
    clock.advance(0.05)
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=10.05,
        adc_time=10.04,
        current_time=10.05,
    )
    assert handler_entered.wait(1.0)
    outcome = pool.lease_outcome("run-a")
    assert outcome.state is LeaseState.COMMITTED
    assert not pool.lease_snapshot("run-a").settled
    assert store.publication_pending(outcome.artifact_id)
    assert pool.snapshot().stream_for("mic-a").owner_count == 0

    shutdown = threading.Thread(target=pool.shutdown)
    shutdown.start()
    time.sleep(0.02)
    assert shutdown.is_alive()
    assert store.publication_pending(outcome.artifact_id)
    handler_decide.set()
    shutdown.join(2.0)
    assert not shutdown.is_alive()
    assert pool.snapshot().state is PoolState.CLOSED
    assert pool.lease_outcome("run-a").state is LeaseState.RELEASED
    assert pool.lease_snapshot("run-a").settled
    assert not store.publication_pending(outcome.artifact_id)
    assert store.reserved_bytes == 0


def test_shutdown_fails_closed_while_terminal_publication_is_unsettled(tmp_path):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    pool._shutdown_control_timeout = 0.02
    handler_entered = threading.Event()
    handler_decide = threading.Event()

    def completion(completion):
        handler_entered.set()
        assert handler_decide.wait(2.0)
        assert completion.decline("late terminal settlement")

    pool.set_completion_handler(completion)
    handle, token = make_ready(pool, resolver, backend)
    pool.reserve("run-a")
    assert pool.commit(
        "run-a", token, request(token, "run-a")
    ).state is LeaseState.COMMITTED
    clock.advance(0.05)
    handle.emit(
        frames=5,
        channels=1,
        callback_monotonic=10.05,
        adc_time=10.04,
        current_time=10.05,
    )
    assert handler_entered.wait(1.0)
    assert pool.lease_outcome("run-a").state is LeaseState.COMMITTED
    assert not pool.lease_snapshot("run-a").settled

    snapshot = pool.shutdown()
    try:
        assert snapshot.state is PoolState.FAILED_CLOSED
        assert snapshot.unsettled_transactions == 1
        assert store.publication_pending(
            pool.lease_outcome("run-a").artifact_id
        )
    finally:
        handler_decide.set()
    wait_until(lambda: pool.lease_snapshot("run-a").settled)
    assert not store.publication_pending(
        pool.lease_outcome("run-a").artifact_id
    )


def test_immutable_snapshots_and_bounded_terminal_diagnostics(tmp_path):
    pool, *_ = make_pool(tmp_path)
    try:
        pool.reserve("one")
        snapshot = pool.snapshot()
        with pytest.raises(AttributeError):
            snapshot.desired_hardware_ids = ("mutated",)
        pool.cancel("one", "done")
        for index in range(300):
            pool.reserve(f"terminal-{index}")
            pool.cancel(f"terminal-{index}", "done")
        assert pool.snapshot().terminal_diagnostic_count <= 256
        assert not hasattr(pool, "release_capacity")
        assert not hasattr(pool, "active_capture_count")
    finally:
        pool.shutdown()
