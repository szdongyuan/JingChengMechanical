"""Task 7 concurrency and latency acceptance gates for Resident recovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import queue
import statistics
import threading
import time
import uuid

import numpy as np

from app.service.analysis.resident_audio_mvp import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    ResidentAudioPool,
    ResidentStreamSpec,
)
from app.service.managed_input_contracts import (
    ManagedHardwareId,
    ResidentStreamGenerationStamp,
)
from app.service.managed_input_resident_adapter import ResidentCaptureTarget
from app.service.captured_start_dispatcher import (
    CapturedStartDispatcher,
    CapturedStartWorkResult,
)
from app.service.multi_run_coordinator_mvp import (
    CaptureReservePending,
    CaptureReserveStatus,
    ManagedResidentCapturePort,
    ManagedResidentCaptureRoute,
    ResidentCancelStatus,
)
from tests.resident_audio_mvp.fakes import FakeBackend


_STREAM_COUNT = 6
_CALLBACKS_PER_SECOND = 20
_WORKLOAD_SECONDS = 60
_CALLBACK_COUNT_PER_STREAM = _CALLBACKS_PER_SECOND * _WORKLOAD_SECONDS
_BLOCK_PERIOD_S = 1.0 / _CALLBACKS_PER_SECOND


def _spec(number: int) -> ResidentStreamSpec:
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
        frames_per_buffer=50,
    )


def _percentile_99(values_ns: list[int]) -> float:
    ordered = sorted(values_ns)
    index = max(0, (len(ordered) * 99 + 99) // 100 - 1)
    return ordered[index] / 1_000_000


def _wait_for(predicate, timeout_s: float, detail: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(detail)
        time.sleep(0.001)


class _ConcurrentWorkloads:
    """Observed downstream pressure kept active for a measured interval."""

    _HISTORY_ROWS = 100_000
    _HISTORY_BATCH_ROWS = 250

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._analysis_ready = threading.Event()
        self._display_ready = threading.Event()
        self._plc_ready = threading.Event()
        self._history_ready = threading.Event()
        self._display_queue: queue.Queue[object] = queue.Queue(maxsize=4)
        self._analysis_starts: list[float] = []
        self._analysis_ends: list[float] = []
        self._analysis_cycles = 0
        self._analysis_delays: list[float] = []
        self._display_producer_start = 0.0
        self._display_producer_end = 0.0
        self._display_start = 0.0
        self._display_end = 0.0
        self._display_processed = 0
        self._display_delays: list[float] = []
        self._display_observer_calls = 0
        self._display_drops = 0
        self._plc_start = 0.0
        self._plc_end = 0.0
        self._plc_calls = 0
        self._plc_delays: list[float] = []
        self._history_start = 0.0
        self._history_end = 0.0
        self._history_rows = 0
        self._history_serialized_bytes = 0
        self._history_checksum = 0
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="analysis-pressure",
        )
        self._analysis_futures = tuple(
            self._executor.submit(self._occupy_analysis_slot)
            for _ in range(2)
        )
        self._threads = (
            threading.Thread(
                target=self._produce_display,
                name="display-pressure-producer",
            ),
            threading.Thread(target=self._slow_display, name="slow-display"),
            threading.Thread(target=self._slow_plc_sink, name="slow-plc-sink"),
            threading.Thread(
                target=self._history_projection_load,
                name="history-100k-load",
            ),
        )
        for thread in self._threads:
            thread.start()

    def accept_display(
        self,
        _hardware_id: str,
        _samples: np.ndarray,
        _frame_count: int,
        _callback_monotonic_ns: int,
        _sample_rate_hz: int,
    ) -> None:
        with self._lock:
            self._display_observer_calls += 1
        self._offer_display()

    def wait_until_ready(self) -> None:
        for event, detail in (
            (self._analysis_ready, "two analysis workers did not occupy their slots"),
            (self._display_ready, "slow display did not receive work"),
            (self._plc_ready, "slow PLC sink did not start"),
            (self._history_ready, "history load did not query and serialize rows"),
        ):
            assert event.wait(5.0), detail

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "analysis_cycles": self._analysis_cycles,
                "display_processed": self._display_processed,
                "display_observer_calls": self._display_observer_calls,
                "plc_calls": self._plc_calls,
                "history_rows": self._history_rows,
                "history_serialized_bytes": self._history_serialized_bytes,
            }

    def stop_and_assert_overlap(
        self,
        *,
        measurement_start: float,
        measurement_end: float,
        before: dict[str, int],
        minimum_observer_calls: int,
    ) -> dict[str, int]:
        self._stop.set()
        for thread in self._threads:
            thread.join(5.0)
            assert not thread.is_alive()
        self._executor.shutdown(wait=True)
        after = self.snapshot()
        assert len(self._analysis_starts) == 2
        assert len(self._analysis_ends) == 2
        assert all(value <= measurement_start for value in self._analysis_starts)
        assert all(value >= measurement_end for value in self._analysis_ends)
        assert self._display_producer_start <= measurement_start
        assert self._display_producer_end >= measurement_end
        assert self._display_start <= measurement_start
        assert self._display_end >= measurement_end
        assert self._plc_start <= measurement_start
        assert self._plc_end >= measurement_end
        assert self._history_start <= measurement_start
        assert self._history_end >= measurement_end
        assert after["analysis_cycles"] - before["analysis_cycles"] >= 2
        assert after["display_processed"] - before["display_processed"] >= 1
        assert after["plc_calls"] - before["plc_calls"] >= 1
        assert after["history_rows"] - before["history_rows"] >= self._HISTORY_ROWS
        assert (
            after["history_serialized_bytes"]
            > before["history_serialized_bytes"]
        )
        assert (
            after["display_observer_calls"] - before["display_observer_calls"]
            >= minimum_observer_calls
        )
        assert self._analysis_delays and min(self._analysis_delays) >= 1.9
        assert self._display_delays and min(self._display_delays) >= 0.45
        assert self._plc_delays and min(self._plc_delays) >= 2.9
        assert self._history_checksum > 0
        return after

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(5.0)
        self._executor.shutdown(wait=True)

    def _occupy_analysis_slot(self) -> None:
        started = time.perf_counter()
        with self._lock:
            self._analysis_starts.append(started)
            if len(self._analysis_starts) == 2:
                self._analysis_ready.set()
        try:
            while not self._stop.is_set():
                cycle_started = time.perf_counter()
                time.sleep(2.0)
                with self._lock:
                    self._analysis_cycles += 1
                    self._analysis_delays.append(
                        time.perf_counter() - cycle_started
                    )
        finally:
            with self._lock:
                self._analysis_ends.append(time.perf_counter())

    def _offer_display(self) -> None:
        try:
            self._display_queue.put_nowait(object())
        except queue.Full:
            try:
                self._display_queue.get_nowait()
            except queue.Empty:
                return
            self._display_queue.task_done()
            with self._lock:
                self._display_drops += 1
            self._display_queue.put_nowait(object())

    def _produce_display(self) -> None:
        self._display_producer_start = time.perf_counter()
        try:
            while not self._stop.is_set():
                self._offer_display()
                time.sleep(0.05)
        finally:
            self._display_producer_end = time.perf_counter()

    def _slow_display(self) -> None:
        self._display_start = time.perf_counter()
        try:
            while not self._stop.is_set():
                try:
                    self._display_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._display_ready.set()
                call_started = time.perf_counter()
                time.sleep(0.5)
                with self._lock:
                    self._display_processed += 1
                    self._display_delays.append(
                        time.perf_counter() - call_started
                    )
                self._display_queue.task_done()
        finally:
            self._display_end = time.perf_counter()

    def _slow_plc_sink(self) -> None:
        self._plc_start = time.perf_counter()
        self._plc_ready.set()
        try:
            while not self._stop.is_set():
                call_started = time.perf_counter()
                time.sleep(3.0)
                with self._lock:
                    self._plc_calls += 1
                    self._plc_delays.append(time.perf_counter() - call_started)
        finally:
            self._plc_end = time.perf_counter()

    def _history_projection_load(self) -> None:
        self._history_start = time.perf_counter()
        next_row = 0
        try:
            while not self._stop.is_set():
                rows = tuple(
                    (
                        row,
                        f"run-{row:06d}",
                        row % 17,
                    )
                    for row in range(
                        next_row,
                        next_row + self._HISTORY_BATCH_ROWS,
                    )
                )
                queried = tuple(row for row in rows if row[2] >= 0)
                payload = json.dumps(queried, separators=(",", ":"))
                with self._lock:
                    self._history_rows += len(queried)
                    self._history_serialized_bytes += len(payload)
                    self._history_checksum += sum(row[2] for row in queried)
                self._history_ready.set()
                next_row = (next_row + self._HISTORY_BATCH_ROWS) % self._HISTORY_ROWS
                time.sleep(0.001)
        finally:
            self._history_end = time.perf_counter()


def test_six_stream_sixty_second_callback_workload_isolated_from_slow_consumers(
) -> None:
    workloads = _ConcurrentWorkloads()
    backend = FakeBackend()
    opened = ResidentAudioPool.open(
        tuple(_spec(number) for number in range(1, _STREAM_COUNT + 1)),
        backend=backend,
        max_capture_frames=1_000,
        startup_stream_timeout_s=1.0,
        fault_close_timeout_s=1.0,
        shutdown_timeout_s=2.0,
        drain_observer=workloads.accept_display,
    )
    assert opened.ready_count == _STREAM_COUNT
    pool = opened.pool
    samples = np.zeros((50, 2), dtype=np.float32)
    callback_durations_ns: list[int] = []
    duration_lock = threading.Lock()
    callback_failures: list[BaseException] = []
    start_barrier = threading.Barrier(_STREAM_COUNT + 1)
    workloads.wait_until_ready()
    workload_before = workloads.snapshot()
    measurement_start = 0.0
    measurement_end = 0.0

    def exercise_stream(number: int) -> None:
        handle = backend.handles[f"mic-{number}"]
        try:
            start_barrier.wait()
            first_deadline = time.perf_counter() + _BLOCK_PERIOD_S
            local_durations: list[int] = []
            for callback_number in range(_CALLBACK_COUNT_PER_STREAM):
                deadline = first_deadline + callback_number * _BLOCK_PERIOD_S
                remaining = deadline - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                started_ns = time.perf_counter_ns()
                directive = handle.callback(
                    samples,
                    50,
                    backend.clock(),
                    BackendCallbackStatus.OK,
                )
                local_durations.append(time.perf_counter_ns() - started_ns)
                assert directive is BackendCallbackDirective.CONTINUE
            with duration_lock:
                callback_durations_ns.extend(local_durations)
        except BaseException as exc:
            callback_failures.append(exc)

    callback_threads = [
        threading.Thread(
            target=exercise_stream,
            args=(number,),
            name=f"callback-workload-{number}",
        )
        for number in range(1, _STREAM_COUNT + 1)
    ]
    try:
        for thread in callback_threads:
            thread.start()
        measurement_start = time.perf_counter()
        start_barrier.wait()
        for thread in callback_threads:
            thread.join(_WORKLOAD_SECONDS + 10.0)
            assert not thread.is_alive()
        assert callback_failures == []
        assert len(callback_durations_ns) == (
            _STREAM_COUNT * _CALLBACK_COUNT_PER_STREAM
        )
        _wait_for(
            lambda: all(
                not slot._ingress.has_pending for slot in pool._slots.values()
            ),
            2.0,
            "normal callback ingress did not drain",
        )
        measurement_end = time.perf_counter()
        p99_ms = _percentile_99(callback_durations_ns)
        max_ms = max(callback_durations_ns) / 1_000_000
        assert p99_ms <= 5.0
        assert max_ms <= 10.0
        assert all(
            slot._ingress.overflow_count == 0
            for slot in pool._slots.values()
        )
        print(
            "six-stream callback metrics: "
            f"count={len(callback_durations_ns)} "
            f"median_ms={statistics.median(callback_durations_ns) / 1_000_000:.4f} "
            f"p99_ms={p99_ms:.4f} max_ms={max_ms:.4f} overflows=0"
        )
    finally:
        if measurement_start and measurement_end:
            workload_after = workloads.stop_and_assert_overlap(
                measurement_start=measurement_start,
                measurement_end=measurement_end,
                before=workload_before,
                minimum_observer_calls=(
                    _STREAM_COUNT * _CALLBACK_COUNT_PER_STREAM
                ),
            )
            print(
                "concurrent workload evidence: "
                f"analysis_cycles={workload_after['analysis_cycles']} "
                f"display_calls={workload_after['display_processed']} "
                f"plc_calls={workload_after['plc_calls']} "
                f"history_rows={workload_after['history_rows']} "
                f"history_json_bytes={workload_after['history_serialized_bytes']}"
            )
        else:
            workloads.close()
        pool.shutdown()


def test_drain_stall_over_two_seconds_faults_as_ingress_overflow() -> None:
    block_next = threading.Event()
    observer_entered = threading.Event()
    observer_release = threading.Event()

    def observer(
        _hardware_id: str,
        _samples: np.ndarray,
        _frame_count: int,
        _callback_monotonic_ns: int,
        _sample_rate_hz: int,
    ) -> None:
        if block_next.is_set():
            observer_entered.set()
            observer_release.wait(5.0)

    backend = FakeBackend()
    opened = ResidentAudioPool.open(
        (_spec(1),),
        backend=backend,
        max_capture_frames=1_000,
        startup_stream_timeout_s=1.0,
        fault_close_timeout_s=1.0,
        shutdown_timeout_s=2.0,
        drain_observer=observer,
    )
    assert opened.ready_count == 1
    pool = opened.pool
    slot = pool._slots["mic-1"]
    handle = backend.handles["mic-1"]
    samples = np.zeros((50, 2), dtype=np.float32)
    block_next.set()
    assert handle.callback(
        samples, 50, backend.clock(), BackendCallbackStatus.OK
    ) is BackendCallbackDirective.CONTINUE
    assert observer_entered.wait(1.0)

    try:
        stall_started = time.monotonic()
        time.sleep(2.05)
        assert time.monotonic() - stall_started > 2.0
        directives = [
            handle.callback(
                samples,
                50,
                backend.clock(),
                BackendCallbackStatus.OK,
            )
            for _ in range(slot._ingress.capacity_blocks)
        ]
        fault, _fault_ns = slot.fault_details_under_decision_lock()

        assert directives[-1] is BackendCallbackDirective.STOP
        assert slot._ingress.overflow_count == 1
        assert fault is not None
        assert "ingress_overflow" in fault.summary
    finally:
        observer_release.set()
        pool.shutdown()


class _PendingLifecycleFuture:
    def __init__(self) -> None:
        self.wait_calls = 0

    def result_if_ready(self):
        return None

    def wait(self, timeout_seconds: float | None):
        self.wait_calls += 1
        if timeout_seconds is not None:
            time.sleep(timeout_seconds)
        return None


class _ParkedReadinessPool:
    def __init__(self) -> None:
        self.future = _PendingLifecycleFuture()
        self.begin_calls = 0

    def begin_readiness(self, _hardware_id: str, _deadline_ns: int):
        self.begin_calls += 1
        return type("PendingTicket", (), {"future": self.future})()


class _TargetAdapter:
    def __init__(self, targets: dict[ManagedHardwareId, ResidentCaptureTarget]) -> None:
        self._targets = targets

    def capture_target(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
    ) -> ResidentCaptureTarget:
        target = self._targets[hardware_id]
        assert target.generation == generation
        return target


def _run_id(number: int) -> str:
    return str(uuid.UUID(int=number, version=4))


def test_healthy_resident_reserve_latency_survives_five_second_parked_recovery(
) -> None:
    workloads = _ConcurrentWorkloads()
    backend = FakeBackend()
    opened = ResidentAudioPool.open(
        (_spec(2),),
        backend=backend,
        max_capture_frames=1_000,
        startup_stream_timeout_s=1.0,
        fault_close_timeout_s=1.0,
        shutdown_timeout_s=2.0,
        drain_observer=workloads.accept_display,
    )
    assert opened.ready_count == 1
    pool = opened.pool
    recovering_id = ManagedHardwareId(1)
    healthy_id = ManagedHardwareId(2)
    recovering_generation = ResidentStreamGenerationStamp(recovering_id, 1, 1)
    healthy_generation = ResidentStreamGenerationStamp(healthy_id, 1, 1)
    recovering_pool = _ParkedReadinessPool()
    adapter = _TargetAdapter(
        {
            recovering_id: ResidentCaptureTarget(
                recovering_pool,
                "mic-recovering",
                recovering_generation,
            ),
            healthy_id: ResidentCaptureTarget(
                pool,
                "mic-2",
                healthy_generation,
            ),
        }
    )
    port = ManagedResidentCapturePort(
        adapter,
        (
            ManagedResidentCaptureRoute(
                "recovering",
                recovering_id,
                recovering_generation,
            ),
            ManagedResidentCaptureRoute(
                "healthy",
                healthy_id,
                healthy_generation,
            ),
        ),
    )
    dispatcher = CapturedStartDispatcher(capacity=2, worker_count=2)
    parked = threading.Event()
    resumed = threading.Event()
    reserve_durations_ns: list[int] = []
    observed_mappings: list[tuple[int, int, int | None, str]] = []
    healthy_worker_names: set[str] = set()
    failures: list[BaseException] = []
    parked_times: list[float] = []
    recovery_results = []
    workloads.wait_until_ready()
    workload_before = workloads.snapshot()
    measurement_start = time.perf_counter()
    measurement_end = 0.0
    recovering_run_id = _run_id(10_000)

    def five_second_recovery() -> CapturedStartWorkResult:
        pending = port.begin_reserve(
            recovering_run_id,
            "recovering",
            950_000_000,
            time.monotonic_ns() + 5_000_000_000,
        )
        assert type(pending) is CaptureReservePending
        parked_times.append(time.perf_counter())
        parked.set()

        def continuation() -> None:
            recovery_results.append(port.resume_reserve(pending.claim))
            parked_times.append(time.perf_counter())
            resumed.set()

        return CapturedStartWorkResult.parked(
            wait=pending.wake_signal.wait,
            continuation=continuation,
            cancel=lambda: port.cancel_reserve(pending.claim),
        )

    try:
        assert dispatcher.try_submit(five_second_recovery, lane_key="recovering")
        assert parked.wait(1.0)
        for index in range(1_000):
            trigger_ns = 950_000_000 + (index % 50) * 1_000_000
            run_id = _run_id(index + 1)
            completion = threading.Event()

            def healthy_start(
                expected_trigger_ns: int = trigger_ns,
                expected_run_id: str = run_id,
            ) -> None:
                try:
                    healthy_worker_names.add(threading.current_thread().name)
                    started_ns = time.perf_counter_ns()
                    result = port.begin_reserve(
                        expected_run_id,
                        "healthy",
                        expected_trigger_ns,
                        time.monotonic_ns() + 5_000_000_000,
                    )
                    reserve_durations_ns.append(
                        time.perf_counter_ns() - started_ns
                    )
                    assert result.status is CaptureReserveStatus.RESERVED
                    reservation = result.reservation
                    assert reservation is not None
                    terminal = threading.Event()
                    port.register_completion(
                        reservation,
                        lambda _outcome: terminal.set(),
                    )
                    port.start(
                        reservation,
                        expected_trigger_ns,
                        target_frame_count=100,
                    )
                    with pool._lock:
                        record = pool._bindings[reservation.reservation_id]
                        observed_mappings.append(
                            (
                                expected_trigger_ns,
                                record.trigger_monotonic_ns,
                                record.start_cursor,
                                record.generation_id,
                            )
                        )
                    assert (
                        port.cancel(reservation).status
                        is ResidentCancelStatus.CANCELLED
                    )
                    assert terminal.wait(1.0)
                    _wait_for(
                        lambda: reservation.reservation_id
                        not in port._reservations,
                        1.0,
                        "cancelled reservation did not retire",
                    )
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    completion.set()

            submit_deadline = time.monotonic() + 1.0
            while not dispatcher.try_submit(healthy_start, lane_key="healthy"):
                assert time.monotonic() < submit_deadline
                time.sleep(0.0005)
            assert completion.wait(1.0)
            assert failures == []

        assert not resumed.is_set(), "healthy work did not finish during the park"
        assert resumed.wait(7.0)
        measurement_end = time.perf_counter()
        p99_ms = _percentile_99(reserve_durations_ns)
        max_ms = max(reserve_durations_ns) / 1_000_000
        assert p99_ms <= 100.0
        assert max_ms <= 250.0
        assert len(observed_mappings) == 1_000
        expected_generation_id = pool._generation_ids["mic-2"]
        assert all(
            mapped_trigger == original_trigger
            and mapped_cursor == (original_trigger - 950_000_000) // 1_000_000
            and generation_id == expected_generation_id
            for (
                original_trigger,
                mapped_trigger,
                mapped_cursor,
                generation_id,
            ) in observed_mappings
        )
        assert recovering_pool.begin_calls == 1
        assert recovering_pool.future.wait_calls > 1
        assert len(recovery_results) == 1
        assert recovery_results[0].status is CaptureReserveStatus.NOT_READY
        assert len(parked_times) == 2
        assert parked_times[1] - parked_times[0] >= 4.9
        assert healthy_worker_names == {"captured-start-dispatch-2"}
        assert pool._capture_permits == 0
        print(
            "healthy Resident reserve metrics: "
            f"count={len(reserve_durations_ns)} "
            f"p99_ms={p99_ms:.4f} max_ms={max_ms:.4f} "
            f"triggers_mapped={len(observed_mappings)} "
            f"parked_seconds={parked_times[1] - parked_times[0]:.4f}"
        )
    finally:
        assert dispatcher.close(timeout_seconds=2.0)
        port.close_and_join(2.0)
        if measurement_end:
            workload_after = workloads.stop_and_assert_overlap(
                measurement_start=measurement_start,
                measurement_end=measurement_end,
                before=workload_before,
                minimum_observer_calls=0,
            )
            print(
                "reserve workload evidence: "
                f"analysis_cycles={workload_after['analysis_cycles']} "
                f"display_calls={workload_after['display_processed']} "
                f"plc_calls={workload_after['plc_calls']} "
                f"history_rows={workload_after['history_rows']}"
            )
        else:
            workloads.close()
        pool.shutdown()
