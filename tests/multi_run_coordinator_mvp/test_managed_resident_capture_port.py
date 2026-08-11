from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
import threading
import uuid

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import (
    CancelResult,
    CancelStatus,
    CaptureBinding,
    CaptureCompletion,
    CapturedAudio,
    CaptureOutcome as ResidentCaptureOutcome,
    CaptureOutcomeStatus as ResidentCaptureOutcomeStatus,
    ResidentAudioPool,
    ResidentError,
    ResidentErrorCode,
    ResidentStreamSpec,
    ShutdownStatus,
)
from app.service.managed_input_contracts import (
    ManagedHardwareId,
    ResidentStreamGenerationStamp,
)
from app.service.managed_input_resident_adapter import ResidentCaptureTarget
from app.service.multi_run_coordinator_mvp import (
    CaptureOutcome,
    CapturedSourceFormat,
    CaptureOutcomeStatus,
    CaptureReserveStatus,
    ManagedResidentCaptureCloseStatus,
    ManagedResidentCaptureCloseReport,
    ManagedResidentCapturePort,
    ManagedResidentCaptureRoute,
    ResidentCancelStatus,
    TerminalErrorCategory,
)
from tests.multi_run_coordinator_mvp._support import (
    RUN_IDS,
    FakeCurrentCollector,
    Harness,
    captured_request,
    completed_capture,
    frozen_captured,
)


def _run_id(index: int = 1) -> str:
    return str(uuid.UUID(int=index, version=4))


def test_every_capture_terminal_stops_and_samples_its_collector_once() -> None:
    statuses = (
        CaptureOutcomeStatus.COMPLETED,
        CaptureOutcomeStatus.FAILED,
        CaptureOutcomeStatus.CANCELLED,
    )
    for index, status in enumerate(statuses):
        harness = Harness()
        collector = FakeCurrentCollector(harness.trace)
        harness.current_factory.collectors.append(collector)
        harness.captured_freezer.result_factory = lambda request: frozen_captured(
            request,
            current_enabled=True,
        )
        coordinator = harness.coordinator()
        assert coordinator.start_captured(
            captured_request(RUN_IDS[index])
        ).accepted
        reservation = harness.resident.register_calls[0]
        if status is CaptureOutcomeStatus.COMPLETED:
            outcome = completed_capture(reservation)
        else:
            outcome = CaptureOutcome(
                run_id=reservation.run_id,
                reservation=reservation,
                status=status,
                audio=None,
                error_category=(
                    TerminalErrorCategory.CAPTURE_FAILED
                    if status is CaptureOutcomeStatus.FAILED
                    else TerminalErrorCategory.CAPTURE_CANCELLED
                ),
                reason=status.name.lower(),
            )

        harness.resident.complete(outcome)

        assert len(collector.stop_calls) == 1
        assert collector.stop_calls[0] is not None
        assert collector.stop_calls[0] >= 0.0
        assert collector.samples_calls == 1
        assert harness.trace.index("current_stop") < harness.trace.index(
            "current_samples"
        )

    for index, stage in enumerate(("stop", "samples", "validate"), start=8):
        harness = Harness()
        collector = FakeCurrentCollector(harness.trace)
        if stage == "stop":
            collector.stop_exception = TimeoutError("stop timed out")
        elif stage == "samples":
            collector.samples_exception = RuntimeError("samples failed")
        else:
            collector.sample_values = [object()]
        harness.current_factory.collectors.append(collector)
        harness.captured_freezer.result_factory = lambda request: frozen_captured(
            request,
            current_enabled=True,
        )
        coordinator = harness.coordinator()
        assert coordinator.start_captured(captured_request(RUN_IDS[index])).accepted
        reservation = harness.resident.register_calls[0]

        harness.resident.complete(completed_capture(reservation))

        terminal = harness.persistence.terminal_calls[-1]
        assert (
            terminal.error_category
            is TerminalErrorCategory.CURRENT_TRACE_INVALID
        )
        assert stage in terminal.reason
        assert len(collector.stop_calls) == 1
        assert collector.samples_calls == 1
        assert harness.persistence.current_calls == []
        assert harness.save_dispatcher.calls == []
        assert harness.scheduler.initial_calls == []


def _binding(
    binding_id: str,
    run_id: str,
    hardware_id: str,
    completion: object | None = None,
) -> CaptureBinding:
    value = object.__new__(CaptureBinding)
    object.__setattr__(value, "binding_id", binding_id)
    object.__setattr__(value, "run_id", run_id)
    object.__setattr__(value, "hardware_id", hardware_id)
    object.__setattr__(value, "_owner_nonce", object())
    object.__setattr__(
        value,
        "_completion",
        completion if completion is not None else CaptureCompletion(),
    )
    return value


class _Completion:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.wait_calls: list[float | None] = []
        self.result_calls = 0
        self._event = threading.Event()
        self.outcome: object | None = None

    def wait(self, timeout_seconds: float | None) -> object | None:
        self.trace.append("wait")
        self.wait_calls.append(timeout_seconds)
        self._event.wait(timeout_seconds)
        return self.outcome

    def result_if_ready(self) -> object | None:
        self.trace.append("result_if_ready")
        self.result_calls += 1
        return self.outcome if self._event.is_set() else None

    def publish(self, outcome: object) -> None:
        self.outcome = outcome
        self._event.set()


class _ReservePool:
    def __init__(self) -> None:
        self.trace: list[str] = []
        self.reserve_calls: list[tuple[str, str]] = []
        self.reserve_entered: threading.Event | None = None
        self.reserve_release: threading.Event | None = None
        self.capture_completion_calls: list[CaptureBinding] = []
        self.start_calls: list[tuple[CaptureBinding, int, int]] = []
        self.first_start_entered: threading.Event | None = None
        self.first_start_release: threading.Event | None = None
        self.cancel_calls: list[CaptureBinding] = []
        self.completions: dict[str, _Completion] = {}
        self.bindings: dict[str, CaptureBinding] = {}
        self.start_outcome_factory = None
        self.start_exception: Exception | None = None
        self.start_return_override: object | None = None
        self.capture_completion_exception: Exception | None = None
        self.lookup_entered: threading.Event | None = None
        self.lookup_release: threading.Event | None = None
        self.barrier_entered: threading.Event | None = None
        self.barrier_release: threading.Event | None = None
        self.cancel_result_factory = None
        self.cancel_exception: Exception | None = None
        self.first_cancel_entered: threading.Event | None = None
        self.first_cancel_release: threading.Event | None = None

    def reserve_capture(self, run_id: str, hardware_id: str) -> CaptureBinding:
        self.trace.append("reserve")
        self.reserve_calls.append((run_id, hardware_id))
        if self.reserve_entered is not None:
            self.reserve_entered.set()
        if self.reserve_release is not None:
            self.reserve_release.wait()
        binding_id = f"binding-{len(self.reserve_calls)}"
        completion = _Completion(self.trace)
        binding = _binding(
            binding_id,
            run_id,
            hardware_id,
        )
        self.completions[binding_id] = completion
        self.bindings[binding_id] = binding
        return binding

    def capture_completion(self, binding: CaptureBinding) -> _Completion:
        self.trace.append("capture_completion")
        self.capture_completion_calls.append(binding)
        call_count = len(
            [
                item
                for item in self.capture_completion_calls
                if item is binding
            ]
        )
        if call_count == 1:
            if self.lookup_entered is not None:
                self.lookup_entered.set()
            if self.lookup_release is not None:
                self.lookup_release.wait()
            if self.capture_completion_exception is not None:
                raise self.capture_completion_exception
        if call_count > 1:
            if self.barrier_entered is not None:
                self.barrier_entered.set()
            if self.barrier_release is not None:
                self.barrier_release.wait()
        return self.completions[binding.binding_id]

    def start_capture(
        self,
        binding: CaptureBinding,
        *,
        trigger_monotonic_ns: int,
        target_frame_count: int,
    ) -> _Completion:
        self.trace.append("start")
        self.start_calls.append(
            (binding, trigger_monotonic_ns, target_frame_count)
        )
        if len(self.start_calls) == 1:
            if self.first_start_entered is not None:
                self.first_start_entered.set()
            if self.first_start_release is not None:
                self.first_start_release.wait()
        if self.start_exception is not None:
            raise self.start_exception
        if self.start_return_override is not None:
            return self.start_return_override  # type: ignore[return-value]
        if self.start_outcome_factory is not None:
            self.completions[binding.binding_id].publish(
                self.start_outcome_factory(binding)
            )
        return self.completions[binding.binding_id]

    def cancel_capture(self, binding: CaptureBinding) -> CancelResult:
        self.trace.append("cancel")
        self.cancel_calls.append(binding)
        if len(self.cancel_calls) == 1:
            if self.first_cancel_entered is not None:
                self.first_cancel_entered.set()
            if self.first_cancel_release is not None:
                self.first_cancel_release.wait()
        if self.cancel_exception is not None:
            raise self.cancel_exception
        if self.cancel_result_factory is not None:
            return self.cancel_result_factory(binding)
        outcome = _resident_cancelled(binding)
        self.completions[binding.binding_id].publish(outcome)
        return CancelResult(CancelStatus.CANCELLED, outcome=outcome)


class _ManagedInput:
    def __init__(
        self,
        targets: dict[
            tuple[ManagedHardwareId, ResidentStreamGenerationStamp],
            ResidentCaptureTarget,
        ],
    ) -> None:
        self.targets = targets
        self.capture_target_calls: list[
            tuple[ManagedHardwareId, ResidentStreamGenerationStamp]
        ] = []
        self.close_all_calls = 0

    def capture_target(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
    ) -> ResidentCaptureTarget:
        self.capture_target_calls.append((hardware_id, generation))
        return self.targets[(hardware_id, generation)]

    def close_all(self, timeout_seconds: float) -> tuple[object, ...]:
        self.close_all_calls += 1
        return ()


def _route(index: int = 1) -> ManagedResidentCaptureRoute:
    managed_id = ManagedHardwareId(index)
    return ManagedResidentCaptureRoute(
        coordinator_hardware_id=f"coordinator-{index}",
        managed_hardware_id=managed_id,
        generation=ResidentStreamGenerationStamp(managed_id, 1, index),
    )


def _port(
    route_count: int = 1,
) -> tuple[ManagedResidentCapturePort, _ManagedInput, list[_ReservePool]]:
    routes = tuple(_route(index) for index in range(1, route_count + 1))
    pools = [_ReservePool() for _ in routes]
    managed_input = _ManagedInput(
        {
            (route.managed_hardware_id, route.generation): ResidentCaptureTarget(
                pool=pool,  # type: ignore[arg-type]
                resident_hardware_id=f"resident-{index}",
                generation=route.generation,
            )
            for index, (route, pool) in enumerate(zip(routes, pools), start=1)
        }
    )
    return ManagedResidentCapturePort(managed_input, routes), managed_input, pools


def test_route_is_the_exact_frozen_value() -> None:
    assert dataclasses.fields(ManagedResidentCaptureRoute) == (
        dataclasses.fields(ManagedResidentCaptureRoute)[0],
        dataclasses.fields(ManagedResidentCaptureRoute)[1],
        dataclasses.fields(ManagedResidentCaptureRoute)[2],
    )
    assert tuple(field.name for field in dataclasses.fields(ManagedResidentCaptureRoute)) == (
        "coordinator_hardware_id",
        "managed_hardware_id",
        "generation",
    )
    assert ManagedResidentCaptureRoute.__dataclass_params__.frozen
    assert ManagedResidentCaptureRoute.__slots__ == (
        "coordinator_hardware_id",
        "managed_hardware_id",
        "generation",
    )


@pytest.mark.parametrize("route_count", range(1, 7))
def test_route_count_from_one_through_six_is_accepted(route_count: int) -> None:
    port, _, _ = _port(route_count)
    assert type(port) is ManagedResidentCapturePort


def test_reserve_rejects_stale_input_before_allocating_capture() -> None:
    route = _route(1)
    pool = _ReservePool()
    managed_input = _ManagedInput(
        {
            (route.managed_hardware_id, route.generation): ResidentCaptureTarget(
                pool=pool,  # type: ignore[arg-type]
                resident_hardware_id="resident-1",
                generation=route.generation,
            )
        }
    )
    observed_hardware_ids = []
    port = ManagedResidentCapturePort(
        managed_input,
        (route,),
        readiness_probe=lambda hardware_id: (
            observed_hardware_ids.append(hardware_id) or False
        ),
    )

    result = port.reserve(_run_id(), route.coordinator_hardware_id)

    assert result.status is CaptureReserveStatus.NOT_READY
    assert result.reason == (
        "音频输入设备未返回采样；已尝试重新启动音频流，"
        "请检查设备连接或重启软件。"
    )
    assert observed_hardware_ids == [route.coordinator_hardware_id]
    assert managed_input.capture_target_calls == []
    assert pool.reserve_calls == []


@pytest.mark.parametrize("route_count", (0, 7))
def test_route_count_outside_one_through_six_is_rejected(route_count: int) -> None:
    routes = tuple(_route(index) for index in range(1, route_count + 1))
    with pytest.raises(ValueError):
        ManagedResidentCapturePort(_ManagedInput({}), routes)


def test_duplicate_coordinator_or_managed_route_is_rejected() -> None:
    first = _route(1)
    second_id = ManagedHardwareId(2)
    duplicate_coordinator = ManagedResidentCaptureRoute(
        first.coordinator_hardware_id,
        second_id,
        ResidentStreamGenerationStamp(second_id, 1, 1),
    )
    duplicate_managed = ManagedResidentCaptureRoute(
        "coordinator-2",
        first.managed_hardware_id,
        first.generation,
    )
    with pytest.raises(ValueError):
        ManagedResidentCapturePort(
            _ManagedInput({}),
            (first, duplicate_coordinator),
        )
    with pytest.raises(ValueError):
        ManagedResidentCapturePort(
            _ManagedInput({}),
            (first, duplicate_managed),
        )


def test_mismatched_generation_and_malformed_routes_are_rejected() -> None:
    first = _route(1)
    with pytest.raises(ValueError):
        ManagedResidentCaptureRoute(
            "coordinator-2",
            ManagedHardwareId(2),
            first.generation,
        )
    with pytest.raises(ValueError):
        ManagedResidentCapturePort(_ManagedInput({}), [first])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ManagedResidentCapturePort(_ManagedInput({}), ("bad",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ManagedResidentCaptureRoute("", first.managed_hardware_id, first.generation)
    with pytest.raises(ValueError):
        ManagedResidentCaptureRoute(
            "coordinator",
            object(),  # type: ignore[arg-type]
            first.generation,
        )


def test_public_constructor_signature_has_no_capacity_option() -> None:
    assert tuple(inspect.signature(ManagedResidentCapturePort).parameters) == (
        "managed_input",
        "routes",
        "readiness_probe",
    )


def test_reserve_routes_opaque_hardware_id_through_public_capture_target() -> None:
    port, managed_input, pools = _port()
    run_id = _run_id()

    result = port.reserve(run_id, "coordinator-1")

    route = _route()
    assert managed_input.capture_target_calls == [
        (route.managed_hardware_id, route.generation)
    ]
    assert pools[0].reserve_calls == [(run_id, "resident-1")]
    assert result.status is CaptureReserveStatus.RESERVED
    assert result.reservation is not None
    assert result.reservation.reservation_id == "binding-1"
    assert result.reservation.run_id == run_id
    assert result.reservation.hardware_id == "coordinator-1"


def test_six_unregistered_reservations_consume_capacity_and_seventh_is_exact_capacity() -> None:
    port, managed_input, pools = _port()

    accepted = [port.reserve(_run_id(index), "coordinator-1") for index in range(1, 7)]
    seventh = port.reserve(_run_id(7), "coordinator-1")

    assert all(result.status is CaptureReserveStatus.RESERVED for result in accepted)
    assert seventh.status is CaptureReserveStatus.CAPACITY
    assert seventh.reservation is None
    assert managed_input.capture_target_calls == [
        (_route().managed_hardware_id, _route().generation)
    ] * 6
    assert len(pools[0].reserve_calls) == 6


def test_unknown_route_and_public_reserve_failures_map_to_closed_results() -> None:
    port, managed_input, _ = _port()
    run_id = _run_id()

    unknown = port.reserve(run_id, "unknown")
    managed_input.targets.clear()
    unavailable = port.reserve(_run_id(2), "coordinator-1")

    assert unknown.status is CaptureReserveStatus.NOT_READY
    assert unknown.reservation is None
    assert unavailable.status is CaptureReserveStatus.NOT_READY
    assert unavailable.reservation is None


def _resident_completed(
    binding: CaptureBinding,
    *,
    frames: int = 3,
    channels: int = 2,
) -> ResidentCaptureOutcome:
    samples = np.array(
        np.arange(frames * channels, dtype=np.float32).reshape(
            frames,
            channels,
        ),
        dtype=np.float32,
        order="C",
        copy=True,
    )
    samples.setflags(write=False)
    audio = CapturedAudio(
        binding=binding,
        run_id=binding.run_id,
        hardware_id=binding.hardware_id,
        trigger_monotonic_ns=1,
        start_frame_cursor=0,
        frame_count=frames,
        sample_rate_hz=48_000,
        channel_indices=tuple(range(channels)),
        samples=samples,
        completion_monotonic_ns=2,
    )
    return ResidentCaptureOutcome(
        status=ResidentCaptureOutcomeStatus.COMPLETED,
        binding=binding,
        audio=audio,
        error=None,
    )


def _resident_cancelled(binding: CaptureBinding) -> ResidentCaptureOutcome:
    return ResidentCaptureOutcome(
        status=ResidentCaptureOutcomeStatus.CANCELLED,
        binding=binding,
        audio=None,
        error=ResidentError(
            ResidentErrorCode.CANCELLED,
            "capture cancelled",
        ),
    )


def _resident_failed(binding: CaptureBinding) -> ResidentCaptureOutcome:
    return ResidentCaptureOutcome(
        status=ResidentCaptureOutcomeStatus.FAILED,
        binding=binding,
        audio=None,
        error=ResidentError(
            ResidentErrorCode.FAULT,
            "capture failed",
        ),
    )


def _reserve_one(
    port: ManagedResidentCapturePort,
    index: int = 1,
) -> tuple[str, object]:
    run_id = _run_id(index)
    result = port.reserve(run_id, "coordinator-1")
    assert result.reservation is not None
    return run_id, result.reservation


def test_register_precedes_start_and_uses_one_builtin_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    created_threads: list[threading.Thread] = []
    real_thread = threading.Thread

    def create_thread(*args: object, **kwargs: object) -> threading.Thread:
        thread = real_thread(*args, **kwargs)
        created_threads.append(thread)
        return thread

    monkeypatch.setattr(threading, "Thread", create_thread)
    delivered = threading.Event()

    port.register_completion(reservation, lambda outcome: delivered.set())
    port.start(reservation, 11, 3)

    assert pool.trace[0] == "reserve"
    assert pool.trace.index("capture_completion") < pool.trace.index("start")
    assert len(created_threads) == 1
    assert type(created_threads[0]) is real_thread
    assert pool.start_calls == [(pool.bindings["binding-1"], 11, 3)]
    pool.completions["binding-1"].publish(
        _resident_completed(pool.bindings["binding-1"])
    )
    assert delivered.wait(1)


def test_duplicate_registration_and_start_are_rejected_without_second_public_call() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)

    port.register_completion(reservation, lambda outcome: None)
    with pytest.raises(ValueError):
        port.register_completion(reservation, lambda outcome: None)
    port.start(reservation, 11, 3)
    with pytest.raises(ValueError):
        port.start(reservation, 11, 3)

    assert len(pool.capture_completion_calls) == 1
    assert len(pool.start_calls) == 1
    port.cancel(reservation)
    assert (
        port.close_and_join(1.0).status
        is ManagedResidentCaptureCloseStatus.CLOSED
    )


def test_two_concurrent_start_callers_make_one_public_start_call() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    port.register_completion(reservation, lambda outcome: None)
    start_entered = threading.Event()
    start_release = threading.Event()
    pool.first_start_entered = start_entered
    pool.first_start_release = start_release
    first_results: list[str] = []
    first_errors: list[Exception] = []

    def first_start() -> None:
        try:
            port.start(reservation, 11, 3)
            first_results.append("started")
        except Exception as exc:
            first_errors.append(exc)

    first_thread = threading.Thread(target=first_start)
    first_thread.start()
    assert start_entered.wait(1)

    second_error: Exception | None = None
    try:
        port.start(reservation, 12, 3)
    except Exception as exc:
        second_error = exc
    call_count_before_release = len(pool.start_calls)
    start_release.set()
    first_thread.join(1)
    port.cancel(reservation)
    close_status = port.close_and_join(1.0).status

    assert type(second_error) is ValueError
    assert call_count_before_release == 1
    assert first_results == ["started"]
    assert first_errors == []
    assert pool.start_calls == [(pool.bindings["binding-1"], 11, 3)]
    assert close_status is ManagedResidentCaptureCloseStatus.CLOSED


def test_close_cannot_settle_while_public_start_call_is_in_flight() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    port.register_completion(reservation, lambda outcome: None)
    start_entered = threading.Event()
    start_release = threading.Event()
    pool.first_start_entered = start_entered
    pool.first_start_release = start_release
    start_errors: list[Exception] = []

    def start() -> None:
        try:
            port.start(reservation, 11, 3)
        except Exception as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=start)
    start_thread.start()
    assert start_entered.wait(1)

    timed_out = port.close_and_join(0.05)
    start_release.set()
    start_thread.join(1)
    clean = port.close_and_join(1.0)

    assert timed_out.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert timed_out.remaining_reservation_ids == (reservation.reservation_id,)
    assert len(pool.start_calls) == 1
    assert len(pool.cancel_calls) == 1
    assert start_errors == []
    assert clean.status is ManagedResidentCaptureCloseStatus.CLOSED


@pytest.mark.parametrize("failure_kind", ("exception", "mismatched-completion"))
def test_failed_start_claim_resolves_without_allowing_retry(
    failure_kind: str,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    port.register_completion(reservation, lambda outcome: None)
    if failure_kind == "exception":
        pool.start_exception = RuntimeError("start failed")
    else:
        pool.start_return_override = object()

    with pytest.raises(RuntimeError):
        port.start(reservation, 11, 3)
    with pytest.raises(ValueError):
        port.start(reservation, 12, 3)

    assert len(pool.start_calls) == 1
    port.cancel(reservation)
    assert (
        port.close_and_join(1.0).status
        is ManagedResidentCaptureCloseStatus.CLOSED
    )


def test_waiter_waits_once_then_reacquires_completion_barrier_before_result_and_sink() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    barrier_entered = threading.Event()
    barrier_release = threading.Event()
    sink_called = threading.Event()
    pool.barrier_entered = barrier_entered
    pool.barrier_release = barrier_release

    port.register_completion(reservation, lambda outcome: sink_called.set())
    pool.completions["binding-1"].publish(
        _resident_completed(pool.bindings["binding-1"])
    )

    assert barrier_entered.wait(1)
    assert not sink_called.is_set()
    assert pool.completions["binding-1"].result_calls == 0
    barrier_release.set()
    assert sink_called.wait(1)
    assert pool.completions["binding-1"].wait_calls == [None]
    assert pool.capture_completion_calls == [
        pool.bindings["binding-1"],
        pool.bindings["binding-1"],
    ]
    assert pool.trace.index("result_if_ready") > max(
        index
        for index, item in enumerate(pool.trace)
        if item == "capture_completion"
    )


def test_synchronous_completion_maps_all_owned_audio_fields() -> None:
    port, _, pools = _port()
    pool = pools[0]
    run_id, reservation = _reserve_one(port)
    delivered: list[object] = []
    delivered_event = threading.Event()
    pool.start_outcome_factory = _resident_completed

    def sink(outcome: object) -> None:
        delivered.append(outcome)
        delivered_event.set()

    port.register_completion(reservation, sink)
    port.start(reservation, 11, 3)

    assert delivered_event.wait(1)
    assert len(delivered) == 1
    outcome = delivered[0]
    assert outcome.status is CaptureOutcomeStatus.COMPLETED
    assert outcome.run_id == run_id
    assert outcome.reservation is reservation
    assert outcome.audio.source_identity == "binding-1"
    assert outcome.audio.sample_rate_hz == 48_000
    assert outcome.audio.channel_count == 2
    assert outcome.audio.frame_count == 3
    assert outcome.audio.format is CapturedSourceFormat.FLOAT32_INTERLEAVED
    assert outcome.audio.interleaved_bytes == np.arange(
        6,
        dtype=np.float32,
    ).tobytes(order="C")
    assert type(outcome.audio.interleaved_bytes) is bytes


def test_ready_waiter_cannot_retire_before_successful_registration_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.service.multi_run_coordinator_mvp.managed_resident_capture_port as adapter

    port, _, pools = _port()
    pool = pools[0]
    start_entered = threading.Event()
    start_release = threading.Event()
    delivered = threading.Event()
    registration_returned = threading.Event()
    registration_errors: list[Exception] = []
    state_transitions: list[str] = []
    real_entry = adapter._ReservationEntry
    real_thread = threading.Thread

    class ObservedEntry(real_entry):
        __slots__ = ()

        def __setattr__(self, name: str, value: object) -> None:
            if name == "state":
                state_transitions.append(value.name)
            super().__setattr__(name, value)

    class StartHeldThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._thread = real_thread(*args, **kwargs)

        def start(self) -> None:
            self._thread.start()
            start_entered.set()
            assert start_release.wait(1)

        def join(self, timeout: float | None = None) -> None:
            self._thread.join(timeout)

        def is_alive(self) -> bool:
            return self._thread.is_alive()

    monkeypatch.setattr(adapter, "_ReservationEntry", ObservedEntry)
    _, reservation = _reserve_one(port)
    pool.completions["binding-1"].publish(
        _resident_completed(pool.bindings["binding-1"])
    )

    def register() -> None:
        try:
            port.register_completion(
                reservation,
                lambda outcome: delivered.set(),
            )
        except Exception as exc:
            registration_errors.append(exc)
        finally:
            registration_returned.set()

    registration = real_thread(target=register)
    monkeypatch.setattr(adapter.threading, "Thread", StartHeldThread)
    registration.start()
    assert start_entered.wait(1)

    assert not delivered.is_set()
    assert not registration_returned.is_set()
    assert reservation.reservation_id in port._reservations
    assert "REGISTERED_WAITING" not in state_transitions
    assert "TERMINAL_DELIVERING" not in state_transitions
    assert "RETIRED" not in state_transitions

    start_release.set()
    assert registration_returned.wait(1)
    registration.join(1)
    assert not registration_errors
    assert delivered.wait(1)
    assert state_transitions.index("REGISTERED_WAITING") < state_transitions.index(
        "TERMINAL_DELIVERING"
    )
    assert state_transitions.index("TERMINAL_DELIVERING") < state_transitions.index(
        "RETIRED"
    )


@pytest.mark.parametrize(
    ("resident_outcome_factory", "expected_status", "expected_category"),
    (
        (
            _resident_cancelled,
            CaptureOutcomeStatus.CANCELLED,
            TerminalErrorCategory.CAPTURE_CANCELLED,
        ),
        (
            _resident_failed,
            CaptureOutcomeStatus.FAILED,
            TerminalErrorCategory.CAPTURE_FAILED,
        ),
        (
            lambda binding: object(),
            CaptureOutcomeStatus.FAILED,
            TerminalErrorCategory.CAPTURE_FAILED,
        ),
    ),
)
def test_closed_completion_mapping(
    resident_outcome_factory,
    expected_status: CaptureOutcomeStatus,
    expected_category: TerminalErrorCategory,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    delivered: list[object] = []
    delivered_event = threading.Event()

    def sink(outcome: object) -> None:
        delivered.append(outcome)
        delivered_event.set()

    port.register_completion(reservation, sink)
    pool.completions["binding-1"].publish(
        resident_outcome_factory(pool.bindings["binding-1"])
    )

    assert delivered_event.wait(1)
    assert len(delivered) == 1
    assert delivered[0].status is expected_status
    assert delivered[0].error_category is expected_category
    assert delivered[0].audio is None
    assert delivered[0].reason


@pytest.mark.parametrize(
    "mutation",
    (
        "completed-with-error",
        "cancelled-with-audio",
        "cancelled-missing-error",
        "cancelled-wrong-code",
        "failed-missing-error",
        "failed-cancel-code",
        "failed-forged-error",
    ),
)
def test_forged_exact_resident_outcome_incoherence_maps_to_failed(
    mutation: str,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    binding = pool.bindings["binding-1"]
    if mutation == "completed-with-error":
        resident_outcome = _resident_completed(binding)
        object.__setattr__(
            resident_outcome,
            "error",
            ResidentError(ResidentErrorCode.FAULT, "unexpected"),
        )
    elif mutation.startswith("cancelled"):
        resident_outcome = _resident_cancelled(binding)
        if mutation == "cancelled-with-audio":
            object.__setattr__(
                resident_outcome,
                "audio",
                _resident_completed(binding).audio,
            )
        elif mutation == "cancelled-missing-error":
            object.__setattr__(resident_outcome, "error", None)
        else:
            object.__setattr__(
                resident_outcome,
                "error",
                ResidentError(ResidentErrorCode.FAULT, "wrong code"),
            )
    else:
        resident_outcome = _resident_failed(binding)
        if mutation == "failed-missing-error":
            object.__setattr__(resident_outcome, "error", None)
        elif mutation == "failed-cancel-code":
            object.__setattr__(
                resident_outcome,
                "error",
                ResidentError(ResidentErrorCode.CANCELLED, "wrong code"),
            )
        else:
            forged_error = object.__new__(ResidentError)
            object.__setattr__(forged_error, "code", object())
            object.__setattr__(forged_error, "summary", "")
            object.__setattr__(resident_outcome, "error", forged_error)
    delivered: list[object] = []
    delivered_event = threading.Event()

    def sink(outcome: object) -> None:
        delivered.append(outcome)
        delivered_event.set()

    port.register_completion(reservation, sink)
    pool.completions["binding-1"].publish(resident_outcome)

    assert delivered_event.wait(1)
    assert delivered[0].status is CaptureOutcomeStatus.FAILED
    assert delivered[0].audio is None
    assert delivered[0].error_category is TerminalErrorCategory.CAPTURE_FAILED


def test_mismatched_completed_audio_maps_to_failed_without_partial_audio() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    delivered: list[object] = []
    delivered_event = threading.Event()
    valid = _resident_completed(pool.bindings["binding-1"])
    malformed = object.__new__(ResidentCaptureOutcome)
    object.__setattr__(malformed, "status", ResidentCaptureOutcomeStatus.COMPLETED)
    object.__setattr__(malformed, "binding", pool.bindings["binding-1"])
    object.__setattr__(malformed, "audio", valid.audio)
    object.__setattr__(malformed, "error", None)
    object.__setattr__(malformed.audio, "run_id", _run_id(999))

    def sink(outcome: object) -> None:
        delivered.append(outcome)
        delivered_event.set()

    port.register_completion(reservation, sink)
    pool.completions["binding-1"].publish(malformed)

    assert delivered_event.wait(1)
    assert len(delivered) == 1
    assert delivered[0].status is CaptureOutcomeStatus.FAILED
    assert delivered[0].audio is None


@pytest.mark.parametrize(
    "mutation",
    ("binding", "run", "hardware", "shape", "indices", "length"),
)
def test_each_completed_audio_identity_and_shape_mismatch_fails_closed(
    mutation: str,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    delivered: list[object] = []
    delivered_event = threading.Event()
    resident_outcome = _resident_completed(pool.bindings["binding-1"])
    audio = resident_outcome.audio
    assert audio is not None
    if mutation == "binding":
        object.__setattr__(
            audio,
            "binding",
            _binding("other", audio.run_id, audio.hardware_id),
        )
    elif mutation == "run":
        object.__setattr__(audio, "run_id", _run_id(99))
    elif mutation == "hardware":
        object.__setattr__(audio, "hardware_id", "other-resident")
    elif mutation == "shape":
        wrong_shape = np.zeros((1, 1), dtype=np.float32)
        wrong_shape.setflags(write=False)
        object.__setattr__(audio, "samples", wrong_shape)
    elif mutation == "indices":
        object.__setattr__(audio, "channel_indices", (1, 0))
    else:
        object.__setattr__(audio, "frame_count", audio.frame_count + 1)

    def sink(outcome: object) -> None:
        delivered.append(outcome)
        delivered_event.set()

    port.register_completion(reservation, sink)
    pool.completions["binding-1"].publish(resident_outcome)

    assert delivered_event.wait(1)
    assert delivered[0].status is CaptureOutcomeStatus.FAILED
    assert delivered[0].audio is None


def test_six_simultaneous_completions_each_deliver_once() -> None:
    port, _, pools = _port()
    pool = pools[0]
    reservations = [_reserve_one(port, index)[1] for index in range(1, 7)]
    delivered: list[object] = []
    delivery_lock = threading.Lock()
    all_delivered = threading.Event()

    def sink(outcome: object) -> None:
        with delivery_lock:
            delivered.append(outcome)
            if len(delivered) == 6:
                all_delivered.set()

    for reservation in reservations:
        port.register_completion(reservation, sink)
    publishers = [
        threading.Thread(
            target=pool.completions[f"binding-{index}"].publish,
            args=(_resident_completed(pool.bindings[f"binding-{index}"]),),
        )
        for index in range(1, 7)
    ]
    for publisher in publishers:
        publisher.start()
    for publisher in publishers:
        publisher.join(1)

    assert all_delivered.wait(1)
    assert len(delivered) == 6
    assert {item.reservation.reservation_id for item in delivered} == {
        f"binding-{index}" for index in range(1, 7)
    }


def test_sink_runs_outside_adapter_lock_and_callback_exception_is_isolated() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, first = _reserve_one(port)
    sink_returned = threading.Event()

    def sink(outcome: object) -> None:
        result = port.reserve(_run_id(2), "unknown")
        assert result.status is CaptureReserveStatus.NOT_READY
        sink_returned.set()
        raise RuntimeError("caller callback failed")

    port.register_completion(first, sink)
    pool.completions["binding-1"].publish(
        _resident_completed(pool.bindings["binding-1"])
    )

    assert sink_returned.wait(1)
    replacement = port.reserve(_run_id(3), "coordinator-1")
    assert replacement.status is CaptureReserveStatus.RESERVED


def test_cancel_before_register_creates_no_waiter_and_retires_capacity() -> None:
    port, _, pools = _port()
    pool = pools[0]
    run_id, reservation = _reserve_one(port)

    result = port.cancel(reservation)

    assert result.status is ResidentCancelStatus.CANCELLED
    assert result.run_id == run_id
    assert result.reservation_id == reservation.reservation_id
    assert pool.capture_completion_calls == []
    assert pool.cancel_calls == [pool.bindings["binding-1"]]
    with pytest.raises(ValueError):
        port.register_completion(reservation, lambda outcome: None)
    assert port.reserve(_run_id(2), "coordinator-1").status is CaptureReserveStatus.RESERVED


@pytest.mark.parametrize(
    ("resident_status", "expected_status"),
    (
        (CancelStatus.ALREADY_TERMINAL, ResidentCancelStatus.ALREADY_TERMINAL),
        (CancelStatus.INVALID, ResidentCancelStatus.INVALID),
    ),
)
def test_exact_public_cancel_status_mapping_retires_unregistered_binding(
    resident_status: CancelStatus,
    expected_status: ResidentCancelStatus,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    binding = pool.bindings["binding-1"]
    if resident_status is CancelStatus.ALREADY_TERMINAL:
        outcome = _resident_cancelled(binding)
        pool.cancel_result_factory = lambda item: CancelResult(
            CancelStatus.ALREADY_TERMINAL,
            outcome=outcome,
        )
    else:
        pool.cancel_result_factory = lambda item: CancelResult(
            CancelStatus.INVALID,
            error=ResidentError(ResidentErrorCode.INVALID, "invalid binding"),
        )

    result = port.cancel(reservation)

    assert result.status is expected_status
    assert result.reason == (
        "invalid binding"
        if expected_status is ResidentCancelStatus.INVALID
        else ""
    )
    assert len(pool.cancel_calls) == 1
    assert port.cancel(reservation).status is ResidentCancelStatus.INVALID
    assert len(pool.cancel_calls) == 1


@pytest.mark.parametrize(
    "mutation",
    (
        "invalid-wrong-code",
        "invalid-with-outcome",
        "cancelled-completed-outcome",
        "cancelled-with-error",
        "already-terminal-malformed-outcome",
    ),
)
def test_forged_exact_cancel_result_is_non_clean(
    mutation: str,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    binding = pool.bindings["binding-1"]
    if mutation.startswith("invalid"):
        result = CancelResult(
            CancelStatus.INVALID,
            error=ResidentError(ResidentErrorCode.INVALID, "invalid"),
        )
        if mutation == "invalid-wrong-code":
            object.__setattr__(
                result,
                "error",
                ResidentError(ResidentErrorCode.FAULT, "wrong code"),
            )
        else:
            object.__setattr__(result, "outcome", _resident_cancelled(binding))
    elif mutation.startswith("cancelled"):
        result = CancelResult(
            CancelStatus.CANCELLED,
            outcome=_resident_cancelled(binding),
        )
        if mutation == "cancelled-completed-outcome":
            object.__setattr__(result, "outcome", _resident_completed(binding))
        else:
            object.__setattr__(
                result,
                "error",
                ResidentError(ResidentErrorCode.FAULT, "unexpected"),
            )
    else:
        malformed = _resident_cancelled(binding)
        object.__setattr__(malformed, "error", None)
        result = CancelResult(
            CancelStatus.ALREADY_TERMINAL,
            outcome=_resident_cancelled(binding),
        )
        object.__setattr__(result, "outcome", malformed)
    pool.cancel_result_factory = lambda item: result

    mapped = port.cancel(reservation)
    report = port.close_and_join(0.05)

    assert mapped.status is ResidentCancelStatus.INVALID
    assert mapped.reason
    assert report.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert report.remaining_reservation_ids == (reservation.reservation_id,)
    assert len(pool.cancel_calls) == 1


def test_cancel_during_registering_does_not_wait_and_waiter_owns_retirement() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    lookup_entered = threading.Event()
    lookup_release = threading.Event()
    pool.lookup_entered = lookup_entered
    pool.lookup_release = lookup_release
    delivered = threading.Event()
    registration_errors: list[Exception] = []

    def register() -> None:
        try:
            port.register_completion(reservation, lambda outcome: delivered.set())
        except Exception as exc:
            registration_errors.append(exc)

    registration = threading.Thread(target=register)
    registration.start()
    assert lookup_entered.wait(1)

    result = port.cancel(reservation)

    assert result.status is ResidentCancelStatus.CANCELLED
    assert registration.is_alive()
    assert len(pool.cancel_calls) == 1
    lookup_release.set()
    registration.join(1)
    assert not registration_errors
    assert delivered.wait(1)
    assert port.reserve(_run_id(2), "coordinator-1").status is CaptureReserveStatus.RESERVED


@pytest.mark.parametrize(
    "failure_stage",
    ("lookup", "thread-construction", "thread-start"),
)
def test_registration_failure_removes_waiter_and_requires_one_exact_cancel(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    sink_called = threading.Event()
    if failure_stage == "lookup":
        pool.capture_completion_exception = RuntimeError("lookup failed")
    elif failure_stage == "thread-construction":
        monkeypatch.setattr(
            threading,
            "Thread",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("construction failed")
            ),
        )
    else:
        class StartFailure:
            def start(self) -> None:
                raise RuntimeError("start failed")

        monkeypatch.setattr(threading, "Thread", lambda *args, **kwargs: StartFailure())

    with pytest.raises(RuntimeError):
        port.register_completion(reservation, lambda outcome: sink_called.set())

    result = port.cancel(reservation)
    assert result.status is ResidentCancelStatus.CANCELLED
    assert len(pool.cancel_calls) == 1
    assert not sink_called.is_set()
    second = port.cancel(reservation)
    assert second.status is ResidentCancelStatus.INVALID
    assert len(pool.cancel_calls) == 1


def test_cancel_result_stored_during_registering_retires_failed_registration() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    lookup_entered = threading.Event()
    lookup_release = threading.Event()
    pool.lookup_entered = lookup_entered
    pool.lookup_release = lookup_release
    pool.capture_completion_exception = RuntimeError("lookup failed")
    registration_failed = threading.Event()

    def register() -> None:
        with pytest.raises(RuntimeError):
            port.register_completion(reservation, lambda outcome: None)
        registration_failed.set()

    registration = threading.Thread(target=register)
    registration.start()
    assert lookup_entered.wait(1)
    assert port.cancel(reservation).status is ResidentCancelStatus.CANCELLED
    lookup_release.set()
    assert registration_failed.wait(1)
    registration.join(1)

    assert port.cancel(reservation).status is ResidentCancelStatus.INVALID
    assert len(pool.cancel_calls) == 1
    assert port.reserve(_run_id(2), "coordinator-1").status is CaptureReserveStatus.RESERVED


@pytest.mark.parametrize("malformed_kind", ("value", "exception"))
def test_malformed_or_exception_cancel_becomes_non_clean(
    malformed_kind: str,
) -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    if malformed_kind == "value":
        pool.cancel_result_factory = lambda binding: object()
    else:
        pool.cancel_exception = RuntimeError("cancel failed")

    result = port.cancel(reservation)
    report = port.close_and_join(0.05)

    assert result.status is ResidentCancelStatus.INVALID
    assert report.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert report.remaining_registration_ids == ()
    assert report.remaining_reservation_ids == (reservation.reservation_id,)
    assert report.remaining_waiter_ids == ()
    assert len(pool.cancel_calls) == 1


def test_close_during_registering_uses_one_cancel_and_names_registration_timeout() -> None:
    port, managed_input, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    lookup_entered = threading.Event()
    lookup_release = threading.Event()
    pool.lookup_entered = lookup_entered
    pool.lookup_release = lookup_release
    registration = threading.Thread(
        target=port.register_completion,
        args=(reservation, lambda outcome: None),
    )
    registration.start()
    assert lookup_entered.wait(1)

    report = port.close_and_join(0.05)

    assert report.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert report.remaining_registration_ids == (reservation.reservation_id,)
    assert report.remaining_reservation_ids == (reservation.reservation_id,)
    assert report.remaining_waiter_ids == ()
    assert len(pool.cancel_calls) == 1
    assert managed_input.close_all_calls == 0
    lookup_release.set()
    registration.join(1)
    clean = port.close_and_join(1.0)
    assert clean.status is ManagedResidentCaptureCloseStatus.CLOSED
    assert len(pool.cancel_calls) == 1


def test_close_cancels_unregistered_and_registered_once_and_joins_cleanly() -> None:
    port, managed_input, pools = _port()
    pool = pools[0]
    _, unregistered = _reserve_one(port, 1)
    _, registered = _reserve_one(port, 2)
    delivered = threading.Event()
    port.register_completion(registered, lambda outcome: delivered.set())

    report = port.close_and_join(1.0)

    assert report.status is ManagedResidentCaptureCloseStatus.CLOSED
    assert report.remaining_registration_ids == ()
    assert report.remaining_reservation_ids == ()
    assert report.remaining_waiter_ids == ()
    assert delivered.is_set()
    assert {binding.binding_id for binding in pool.cancel_calls} == {
        unregistered.reservation_id,
        registered.reservation_id,
    }
    assert len(pool.cancel_calls) == 2
    assert managed_input.close_all_calls == 0
    assert port.close_and_join(1.0) is report
    assert len(pool.cancel_calls) == 2


def test_close_waits_for_preclose_reserve_and_late_binding_is_cancelled_once() -> None:
    port, managed_input, pools = _port()
    pool = pools[0]
    reserve_entered = threading.Event()
    reserve_release = threading.Event()
    close_returned = threading.Event()
    pool.reserve_entered = reserve_entered
    pool.reserve_release = reserve_release
    reserve_results: list[object] = []
    close_reports: list[ManagedResidentCaptureCloseReport] = []
    reserve_thread = threading.Thread(
        target=lambda: reserve_results.append(
            port.reserve(_run_id(), "coordinator-1")
        )
    )
    reserve_thread.start()
    assert reserve_entered.wait(1)

    def close() -> None:
        close_reports.append(port.close_and_join(1.0))
        close_returned.set()

    close_thread = threading.Thread(target=close)
    close_thread.start()
    closed_prematurely = close_returned.wait(0.05)

    reserve_release.set()
    reserve_thread.join(1)
    close_thread.join(1)

    assert not closed_prematurely
    assert close_returned.is_set()
    assert reserve_results[0].status is CaptureReserveStatus.CLOSING
    assert close_reports[0].status is ManagedResidentCaptureCloseStatus.CLOSED
    assert len(pool.cancel_calls) == 1
    assert pool.cancel_calls[0].binding_id == "binding-1"
    assert managed_input.close_all_calls == 0
    assert port.close_and_join(1.0) is close_reports[0]
    assert len(pool.cancel_calls) == 1


def test_close_timeout_names_inflight_reserve_claim_then_reobserves_without_retry() -> None:
    port, _, pools = _port()
    pool = pools[0]
    run_id = _run_id()
    reserve_entered = threading.Event()
    reserve_release = threading.Event()
    pool.reserve_entered = reserve_entered
    pool.reserve_release = reserve_release
    reserve_results: list[object] = []
    reserve_thread = threading.Thread(
        target=lambda: reserve_results.append(
            port.reserve(run_id, "coordinator-1")
        )
    )
    reserve_thread.start()
    assert reserve_entered.wait(1)

    timed_out = port.close_and_join(0.05)
    cancel_calls_at_timeout = len(pool.cancel_calls)
    reserve_release.set()
    clean = port.close_and_join(1.0)
    reserve_thread.join(1)

    assert timed_out.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert timed_out.remaining_registration_ids == ()
    assert timed_out.remaining_reservation_ids == (run_id,)
    assert timed_out.remaining_waiter_ids == ()
    assert cancel_calls_at_timeout == 0
    assert clean.status is ManagedResidentCaptureCloseStatus.CLOSED
    assert reserve_results[0].status is CaptureReserveStatus.CLOSING
    assert len(pool.cancel_calls) == 1
    assert port.close_and_join(1.0) is clean
    assert len(pool.cancel_calls) == 1


def test_late_close_cancel_dispatches_every_frozen_cancel_without_wait_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.service.multi_run_coordinator_mvp.managed_resident_capture_port as adapter

    class Monotonic:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Monotonic()
    monkeypatch.setattr(adapter.time, "monotonic", clock)
    port, _, pools = _port()
    pool = pools[0]
    _, first = _reserve_one(port, 1)
    _, second = _reserve_one(port, 2)
    second_delivered = threading.Event()
    port.register_completion(second, lambda outcome: second_delivered.set())
    pool.cancel_result_factory = lambda binding: CancelResult(
        CancelStatus.CANCELLED,
        outcome=_resident_cancelled(binding),
    )
    second_waiter = port._reservations[second.reservation_id].waiter
    assert second_waiter is not None
    condition_wait_calls: list[float | None] = []
    waiter_join_calls: list[float | None] = []
    original_waiter_join = second_waiter.join
    monkeypatch.setattr(
        port._condition,
        "wait",
        lambda timeout=None: condition_wait_calls.append(timeout),
    )
    monkeypatch.setattr(
        second_waiter,
        "join",
        lambda timeout=None: waiter_join_calls.append(timeout),
    )
    cancel_entered = threading.Event()
    cancel_release = threading.Event()
    pool.first_cancel_entered = cancel_entered
    pool.first_cancel_release = cancel_release
    close_reports: list[ManagedResidentCaptureCloseReport] = []
    close_returned = threading.Event()

    def close() -> None:
        close_reports.append(port.close_and_join(1.0))
        close_returned.set()

    close_thread = threading.Thread(target=close)
    close_thread.start()
    try:
        assert cancel_entered.wait(1)
        clock.now = 102.0
        cancel_release.set()
        assert close_returned.wait(1)
        close_thread.join(1)

        first_report = close_reports[0]
        pool.completions[second.reservation_id].publish(
            _resident_cancelled(pool.bindings[second.reservation_id])
        )
        assert second_delivered.wait(1)
        assert first_report.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
        assert first_report.remaining_registration_ids == ()
        assert first_report.remaining_reservation_ids == (
            first.reservation_id,
            second.reservation_id,
        )
        assert first_report.remaining_waiter_ids == (second.reservation_id,)
        assert [binding.binding_id for binding in pool.cancel_calls] == [
            first.reservation_id,
            second.reservation_id,
        ]
        assert condition_wait_calls == []
        assert waiter_join_calls == []

        repeated = port.close_and_join(1.0)
        assert repeated.status is ManagedResidentCaptureCloseStatus.CLOSED
        assert repeated.remaining_registration_ids == ()
        assert repeated.remaining_reservation_ids == ()
        assert repeated.remaining_waiter_ids == ()
        assert port.close_and_join(1.0) is repeated
        assert [binding.binding_id for binding in pool.cancel_calls] == [
            first.reservation_id,
            second.reservation_id,
        ]
    finally:
        cancel_release.set()
        pool.completions[second.reservation_id].publish(
            _resident_cancelled(pool.bindings[second.reservation_id])
        )
        close_thread.join(1)
        original_waiter_join(1)


def test_blocked_sink_is_exact_non_clean_waiter_and_repeated_close_only_observes() -> None:
    port, _, pools = _port()
    pool = pools[0]
    _, reservation = _reserve_one(port)
    sink_entered = threading.Event()
    sink_release = threading.Event()

    def sink(outcome: object) -> None:
        sink_entered.set()
        sink_release.wait()

    port.register_completion(reservation, sink)
    pool.completions["binding-1"].publish(
        _resident_completed(pool.bindings["binding-1"])
    )
    assert sink_entered.wait(1)

    first = port.close_and_join(0.05)
    second = port.close_and_join(0.05)

    assert first.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert first.remaining_registration_ids == ()
    assert first.remaining_reservation_ids == (reservation.reservation_id,)
    assert first.remaining_waiter_ids == (reservation.reservation_id,)
    assert second.status is ManagedResidentCaptureCloseStatus.NON_CLEAN
    assert len(pool.cancel_calls) <= 1
    cancel_count = len(pool.cancel_calls)
    sink_release.set()
    clean = port.close_and_join(1.0)
    assert clean.status is ManagedResidentCaptureCloseStatus.CLOSED
    assert len(pool.cancel_calls) == cancel_count


@pytest.mark.parametrize(
    "timeout_seconds",
    (0, -1, 61, float("inf"), float("nan"), "1"),
)
def test_close_timeout_must_be_finite_positive_and_bounded(timeout_seconds) -> None:
    port, _, _ = _port()
    with pytest.raises(ValueError):
        port.close_and_join(timeout_seconds)


def test_close_report_is_exact_frozen_bounded_value() -> None:
    report = ManagedResidentCaptureCloseReport(
        status=ManagedResidentCaptureCloseStatus.NON_CLEAN,
        remaining_registration_ids=("binding-1",),
        remaining_reservation_ids=("binding-1",),
        remaining_waiter_ids=(),
    )
    assert dataclasses.is_dataclass(report)
    assert report.__dataclass_params__.frozen
    with pytest.raises(ValueError):
        ManagedResidentCaptureCloseReport(
            status=ManagedResidentCaptureCloseStatus.CLOSED,
            remaining_registration_ids=("binding-1",),
            remaining_reservation_ids=("binding-1",),
            remaining_waiter_ids=(),
        )


def test_real_frozen_resident_pool_completion_crosses_public_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.resident_audio_mvp.fakes import (
        FakeBackend,
        ManualMonotonicClock,
    )

    clock = ManualMonotonicClock(initial_ns=1_000_000_000)
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns",
        clock,
    )
    backend = FakeBackend(clock=clock)
    opened = ResidentAudioPool.open(
        (
            ResidentStreamSpec(
                hardware_id="real-resident",
                device_index=1,
                device_name="Exact Input",
                hostapi_index=0,
                hostapi_name="WASAPI",
                sample_rate_hz=10,
                input_channel_count=2,
                capture_channel_indices=(0, 1),
                sample_format="float32",
                frames_per_buffer=2,
            ),
        ),
        backend=backend,
        max_capture_frames=10,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 1
    route = _route()
    managed_input = _ManagedInput(
        {
            (route.managed_hardware_id, route.generation): ResidentCaptureTarget(
                pool=opened.pool,
                resident_hardware_id="real-resident",
                generation=route.generation,
            )
        }
    )
    port = ManagedResidentCapturePort(managed_input, (route,))
    _, reservation = _reserve_one(port)
    delivered: list[object] = []
    delivered_event = threading.Event()

    def sink(outcome: object) -> None:
        delivered.append(outcome)
        delivered_event.set()

    port.register_completion(reservation, sink)
    try:
        port.start(
            reservation,
            trigger_monotonic_ns=1_100_000_000,
            target_frame_count=2,
        )
        backend.handles["real-resident"].emit_numbered_frames(first=2, count=4)

        assert delivered_event.wait(1)
        assert delivered[0].status is CaptureOutcomeStatus.COMPLETED
        assert delivered[0].audio.source_identity == reservation.reservation_id
        assert delivered[0].audio.channel_count == 2
    finally:
        port.cancel(reservation)
        assert (
            port.close_and_join(1.0).status
            is ManagedResidentCaptureCloseStatus.CLOSED
        )
        assert opened.pool.shutdown().status is ShutdownStatus.CLOSED


def test_static_adapter_uses_only_public_managed_resident_seams_and_one_waiter() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "app"
        / "service"
        / "multi_run_coordinator_mvp"
        / "managed_resident_capture_port.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_import_fragments = (
        "resident_audio_mvp.contracts",
        "resident_audio_mvp.pool",
        "resident_audio_mvp.stream",
        "resident_audio_mvp.ring",
    )
    imported_modules = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ]
    assert not any(
        fragment in module
        for module in imported_modules
        for fragment in forbidden_import_fragments
    )
    assert source.count("threading.Thread(") == 1
    assert "ThreadPoolExecutor" not in source
    assert "Executor" not in source
    assert "Queue" not in source
    assert "deque" not in source
    for forbidden in (
        ".sleep(",
        "retry",
        "recovery",
        "backoff",
        "event_bus",
        "proxy",
        "thread_local",
    ):
        assert forbidden not in source.lower()

    waiter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_wait_for_completion"
    )
    assert not any(
        isinstance(node, (ast.For, ast.While))
        for node in ast.walk(waiter)
    )
    wait_calls = [
        node
        for node in ast.walk(waiter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait"
    ]
    assert len(wait_calls) == 1
    assert ast.unparse(wait_calls[0]) == "completion.wait(None)"
    assert "pool.capture_completion(binding)" in ast.unparse(waiter)
    assert "completion.result_if_ready()" in ast.unparse(waiter)
    public_object_names = {
        "managed_input",
        "target",
        "pool",
        "binding",
        "completion",
        "barrier_completion",
        "resident_outcome",
    }
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in public_object_names
        and node.attr.startswith("_")
    ]


def test_static_capacity_and_public_constructor_are_fixed_at_six() -> None:
    assert tuple(inspect.signature(ManagedResidentCapturePort).parameters) == (
        "managed_input",
        "routes",
        "readiness_probe",
    )
    module = inspect.getmodule(ManagedResidentCapturePort)
    assert module is not None
    source = inspect.getsource(module)
    assert "_MAX_RESERVATIONS = 6" in source
    assert "max_workers" not in source


def test_adapter_values_are_direct_coordinator_package_attributes() -> None:
    import app.service.multi_run_coordinator_mvp as package

    assert package.ManagedResidentCapturePort is ManagedResidentCapturePort
    assert package.ManagedResidentCaptureRoute is ManagedResidentCaptureRoute
    assert (
        package.ManagedResidentCaptureCloseReport
        is ManagedResidentCaptureCloseReport
    )
    assert (
        package.ManagedResidentCaptureCloseStatus
        is ManagedResidentCaptureCloseStatus
    )
