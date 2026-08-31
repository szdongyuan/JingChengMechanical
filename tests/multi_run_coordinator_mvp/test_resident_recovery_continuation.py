from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import uuid

import pytest

from app.service.analysis.resident_audio_mvp import (
    CancelResult,
    CancelStatus,
    CaptureBinding,
    CaptureCompletion,
    CaptureOutcome,
    CaptureOutcomeStatus,
    DiagnosticReason,
    DiagnosticStage,
    LifecycleOutcome,
    LifecycleStatus,
    ResidentDiagnostic,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
)
from app.service.managed_input_contracts import (
    ManagedHardwareId,
    ResidentStreamGenerationStamp,
)
from app.service.managed_input_resident_adapter import ResidentCaptureTarget
from app.service.multi_run_coordinator_mvp import (
    CaptureReservePending,
    CaptureReserveStatus,
    ManagedResidentCaptureCloseStatus,
    ManagedResidentCapturePort,
    ManagedResidentCaptureRoute,
    ResidentCancelStatus,
)


def _run_id(index: int) -> str:
    return str(uuid.UUID(int=index, version=4))


def _diagnostic(
    stage: DiagnosticStage,
    reason: DiagnosticReason,
    detail: str,
) -> ResidentDiagnostic:
    return ResidentDiagnostic(stage, reason, detail)


class _LifecycleFuture:
    def __init__(self, outcome: LifecycleOutcome | None = None) -> None:
        self._outcome = outcome
        self._event = threading.Event()
        if outcome is not None:
            self._event.set()
        self.wait_entered = threading.Event()
        self.wait_timeouts: list[float | None] = []
        self.wait_exception: Exception | None = None

    def result_if_ready(self) -> LifecycleOutcome | None:
        return self._outcome if self._event.is_set() else None

    def wait(self, timeout_seconds: float | None) -> LifecycleOutcome | None:
        self.wait_timeouts.append(timeout_seconds)
        self.wait_entered.set()
        if self.wait_exception is not None:
            raise self.wait_exception
        if not self._event.wait(timeout_seconds):
            return None
        return self._outcome

    def settle(self, outcome: LifecycleOutcome) -> None:
        self._outcome = outcome
        self._event.set()


@dataclass(frozen=True)
class _ReadinessTicket:
    future: _LifecycleFuture


def _binding(binding_id: str, run_id: str, hardware_id: str) -> CaptureBinding:
    binding = object.__new__(CaptureBinding)
    object.__setattr__(binding, "binding_id", binding_id)
    object.__setattr__(binding, "run_id", run_id)
    object.__setattr__(binding, "hardware_id", hardware_id)
    object.__setattr__(binding, "_owner_nonce", object())
    object.__setattr__(binding, "_completion", CaptureCompletion())
    return binding


class _RecoveryPool:
    def __init__(self, futures: list[_LifecycleFuture]) -> None:
        self.futures = futures
        self.begin_calls: list[tuple[str, int]] = []
        self.reserve_calls: list[tuple[str, str, str | None]] = []
        self.cancel_calls: list[CaptureBinding] = []
        self.bindings: dict[str, CaptureBinding] = {}
        self.reserve_exception: Exception | None = None

    def begin_readiness(
        self,
        hardware_id: str,
        deadline_monotonic_ns: int,
    ) -> _ReadinessTicket:
        self.begin_calls.append((hardware_id, deadline_monotonic_ns))
        return _ReadinessTicket(self.futures.pop(0))

    def reserve_capture(
        self,
        run_id: str,
        hardware_id: str,
        *,
        generation_id: str | None = None,
    ) -> CaptureBinding:
        self.reserve_calls.append((run_id, hardware_id, generation_id))
        if self.reserve_exception is not None:
            raise self.reserve_exception
        binding = _binding(
            f"binding-{len(self.reserve_calls)}",
            run_id,
            hardware_id,
        )
        self.bindings[binding.binding_id] = binding
        return binding

    def cancel_capture(self, binding: CaptureBinding) -> CancelResult:
        self.cancel_calls.append(binding)
        return CancelResult(
            CancelStatus.CANCELLED,
            outcome=CaptureOutcome(
                CaptureOutcomeStatus.CANCELLED,
                binding,
                None,
                ResidentError(ResidentErrorCode.CANCELLED, "cancelled"),
            ),
        )


class _ManagedInput:
    def __init__(self, targets: dict[ManagedHardwareId, ResidentCaptureTarget]) -> None:
        self.targets = targets
        self.capture_target_calls: list[
            tuple[ManagedHardwareId, ResidentStreamGenerationStamp]
        ] = []

    def capture_target(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
    ) -> ResidentCaptureTarget:
        self.capture_target_calls.append((hardware_id, generation))
        return self.targets[hardware_id]


def _port(
    pools: tuple[_RecoveryPool, ...],
) -> tuple[ManagedResidentCapturePort, tuple[ManagedResidentCaptureRoute, ...]]:
    routes = tuple(
        ManagedResidentCaptureRoute(
            coordinator_hardware_id=f"coordinator-{index}",
            managed_hardware_id=ManagedHardwareId(index),
            generation=ResidentStreamGenerationStamp(
                ManagedHardwareId(index),
                1,
                index,
            ),
        )
        for index in range(1, len(pools) + 1)
    )
    targets = {
        route.managed_hardware_id: ResidentCaptureTarget(
            pool=pool,  # type: ignore[arg-type]
            resident_hardware_id=f"resident-{index}",
            generation=route.generation,
        )
        for index, (route, pool) in enumerate(zip(routes, pools), start=1)
    }
    return ManagedResidentCapturePort(_ManagedInput(targets), routes), routes


def _continuous(generation_id: str) -> LifecycleOutcome:
    return LifecycleOutcome(
        LifecycleStatus.GENERATION_CONTINUOUS,
        generation_id=generation_id,
    )


def _begin(
    port: ManagedResidentCapturePort,
    index: int,
    hardware_id: str = "coordinator-1",
):
    return port.begin_reserve(
        _run_id(index),
        hardware_id,
        trigger_monotonic_ns=1_000_000_000 + index,
        deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
    )


def test_immediate_continuous_generation_is_pinned_without_mapping_trigger() -> None:
    pool = _RecoveryPool([_LifecycleFuture(_continuous("resident-1:generation-7"))])
    port, _ = _port((pool,))

    result = _begin(port, 1)

    assert result.status is CaptureReserveStatus.RESERVED
    assert result.reservation is not None
    assert pool.reserve_calls == [
        (_run_id(1), "resident-1", "resident-1:generation-7")
    ]
    assert not hasattr(pool, "start_calls")


def test_pending_resume_uses_the_same_authenticated_claim_and_generation() -> None:
    future = _LifecycleFuture()
    pool = _RecoveryPool([future])
    port, _ = _port((pool,))

    pending = _begin(port, 1)
    assert type(pending) is CaptureReservePending
    assert pool.begin_calls[0][0] == "resident-1"
    assert pool.reserve_calls == []
    future.settle(_continuous("resident-1:generation-8"))

    result = port.resume_reserve(pending.claim)

    assert result.status is CaptureReserveStatus.RESERVED
    assert pool.begin_calls == [pool.begin_calls[0]]
    assert pool.reserve_calls == [
        (_run_id(1), "resident-1", "resident-1:generation-8")
    ]


def test_pending_wake_signal_observes_settlement_without_advancing_claim() -> None:
    future = _LifecycleFuture()
    pool = _RecoveryPool([future])
    port, _ = _port((pool,))

    pending = _begin(port, 1)
    wait_returned = threading.Event()
    waiter = threading.Thread(
        target=lambda: (
            pending.wake_signal.wait(),
            wait_returned.set(),
        )
    )
    waiter.start()
    assert future.wait_entered.wait(1.0)
    assert pool.reserve_calls == []

    future.settle(_continuous("resident-1:generation-9"))
    assert wait_returned.wait(1.0)
    assert pool.reserve_calls == []

    result = port.resume_reserve(pending.claim)
    waiter.join(1.0)
    assert result.status is CaptureReserveStatus.RESERVED
    assert pool.reserve_calls == [
        (_run_id(1), "resident-1", "resident-1:generation-9")
    ]


def test_pending_wake_signal_returns_when_the_same_claim_is_cancelled() -> None:
    future = _LifecycleFuture()
    pool = _RecoveryPool([future])
    port, _ = _port((pool,))
    pending = _begin(port, 1)
    wait_returned = threading.Event()
    waiter = threading.Thread(
        target=lambda: (
            pending.wake_signal.wait(),
            wait_returned.set(),
        )
    )
    waiter.start()
    assert future.wait_entered.wait(1.0)

    assert port.cancel_reserve(pending.claim) is True
    assert wait_returned.wait(1.0)
    waiter.join(1.0)
    assert pool.reserve_calls == []


def test_generation_replacement_fails_this_claim_with_precise_compatible_detail() -> None:
    replacement = LifecycleOutcome(
        LifecycleStatus.GENERATION_REPLACED,
        generation_id="resident-1:generation-2",
        diagnostic=_diagnostic(
            DiagnosticStage.PUBLISH_CANDIDATE,
            DiagnosticReason.GENERATION_REPLACED_FOR_TRIGGER,
            "new generation published",
        ),
    )
    pool = _RecoveryPool([_LifecycleFuture(replacement)])
    port, _ = _port((pool,))

    result = _begin(port, 1)

    assert result.status is CaptureReserveStatus.NOT_READY
    assert "publish_candidate" in result.reason
    assert "generation_replaced_for_trigger" in result.reason
    assert "new generation published" in result.reason
    assert pool.reserve_calls == []


def test_generation_change_while_pinning_maps_to_precise_replacement_detail() -> None:
    pool = _RecoveryPool(
        [_LifecycleFuture(_continuous("resident-1:generation-1"))]
    )
    pool.reserve_exception = ResidentRequestError(
        ResidentError(ResidentErrorCode.NOT_READY, "generation changed")
    )
    port, _ = _port((pool,))

    result = _begin(port, 1)

    assert result.status is CaptureReserveStatus.NOT_READY
    assert "publish_candidate" in result.reason
    assert "generation_replaced_for_trigger" in result.reason
    assert "generation changed" in result.reason


@pytest.mark.parametrize(
    ("status", "stage", "reason"),
    (
        (
            LifecycleStatus.RECOVERY_FAILED,
            DiagnosticStage.RESOLVE_ENDPOINT,
            DiagnosticReason.ENDPOINT_MISSING,
        ),
        (
            LifecycleStatus.RECOVERY_PENDING,
            DiagnosticStage.BACKGROUND_RETRY,
            DiagnosticReason.ORPHAN_LIMIT_REACHED,
        ),
    ),
)
def test_recovery_endpoint_and_orphan_failures_keep_precise_not_ready_detail(
    status: LifecycleStatus,
    stage: DiagnosticStage,
    reason: DiagnosticReason,
) -> None:
    outcome = LifecycleOutcome(
        status,
        diagnostic=_diagnostic(stage, reason, "bounded recovery detail"),
    )
    port, _ = _port((_RecoveryPool([_LifecycleFuture(outcome)]),))

    result = _begin(port, 1)

    assert result.status is CaptureReserveStatus.NOT_READY
    assert stage.value in result.reason
    assert reason.value in result.reason
    assert "bounded recovery detail" in result.reason


def test_pending_claim_owns_capacity_duplicate_is_rejected_and_failure_releases_once() -> None:
    first_future = _LifecycleFuture()
    futures = [first_future, *[_LifecycleFuture() for _ in range(6)]]
    pool = _RecoveryPool(futures)
    port, _ = _port((pool,))

    pending = [_begin(port, index) for index in range(1, 7)]
    duplicate = _begin(port, 1)
    seventh = _begin(port, 7)

    assert all(type(item) is CaptureReservePending for item in pending)
    assert duplicate.status is CaptureReserveStatus.FAULT
    assert "duplicate_run_id" in duplicate.reason
    assert seventh.status is CaptureReserveStatus.CAPACITY
    assert "capture_capacity" in seventh.reason
    assert len(pool.begin_calls) == 6

    first = pending[0]
    first_future.settle(
        LifecycleOutcome(
            LifecycleStatus.RECOVERY_FAILED,
            diagnostic=_diagnostic(
                DiagnosticStage.OPEN_CANDIDATE,
                DiagnosticReason.OPEN_FAILED,
                "open failed",
            ),
        )
    )
    failed = port.resume_reserve(first.claim)
    replacement = _begin(port, 7)

    assert failed.status is CaptureReserveStatus.NOT_READY
    assert type(replacement) is CaptureReservePending
    with pytest.raises(ValueError):
        port.resume_reserve(first.claim)


def test_lifecycle_cancel_and_close_release_waiting_capacity_exactly_once() -> None:
    cancelled = LifecycleOutcome(
        LifecycleStatus.LIFECYCLE_CANCELLED,
        diagnostic=_diagnostic(
            DiagnosticStage.LIFECYCLE,
            DiagnosticReason.CLOSING,
            "new revision owns cancellation",
        ),
    )
    first_future = _LifecycleFuture()
    second_future = _LifecycleFuture()
    pool = _RecoveryPool([first_future, second_future])
    port, _ = _port((pool,))
    first = _begin(port, 1)
    second = _begin(port, 2)
    first_future.settle(cancelled)

    result = port.resume_reserve(first.claim)
    with pytest.raises(ValueError):
        port.resume_reserve(first.claim)
    report = port.close_and_join(1.0)

    assert result.status is CaptureReserveStatus.CLOSING
    assert "new revision owns cancellation" in result.reason
    assert report.status is ManagedResidentCaptureCloseStatus.CLOSED
    assert pool.reserve_calls == []
    closed = port.resume_reserve(second.claim)
    assert closed.status is CaptureReserveStatus.CLOSING
    assert port.close_and_join(1.0) is report


def test_explicit_pending_claim_cancel_releases_capacity_exactly_once() -> None:
    futures = [
        _LifecycleFuture(),
        *[
            _LifecycleFuture(_continuous("resident-1:generation-1"))
            for _ in range(6)
        ],
    ]
    pool = _RecoveryPool(futures)
    port, _ = _port((pool,))
    pending = _begin(port, 1)

    assert port.cancel_reserve(pending.claim) is True
    assert port.cancel_reserve(pending.claim) is False
    accepted = [_begin(port, index) for index in range(2, 8)]

    assert all(item.status is CaptureReserveStatus.RESERVED for item in accepted)
    assert _begin(port, 8).status is CaptureReserveStatus.CAPACITY
    assert pool.reserve_calls == [
        (_run_id(index), "resident-1", "resident-1:generation-1")
        for index in range(2, 8)
    ]


def test_forged_claim_is_rejected_without_consuming_the_owned_claim() -> None:
    future = _LifecycleFuture()
    pool = _RecoveryPool([future])
    port, _ = _port((pool,))
    pending = _begin(port, 1)
    forged = object.__new__(type(pending.claim))
    for field_name in (
        "claim_id",
        "run_id",
        "hardware_id",
        "trigger_monotonic_ns",
        "deadline_monotonic_ns",
    ):
        object.__setattr__(forged, field_name, getattr(pending.claim, field_name))
    object.__setattr__(forged, "_owner_nonce", object())

    with pytest.raises(ValueError):
        port.resume_reserve(forged)

    future.settle(_continuous("resident-1:generation-9"))
    assert port.resume_reserve(pending.claim).status is CaptureReserveStatus.RESERVED


def test_resume_exception_releases_claim_capacity_once() -> None:
    first_future = _LifecycleFuture()
    first_future.wait_exception = RuntimeError("readiness wait failed")
    other_futures = [
        _LifecycleFuture(_continuous(f"resident-1:generation-{index}"))
        for index in range(2, 8)
    ]
    pool = _RecoveryPool([first_future, *other_futures])
    port, _ = _port((pool,))
    pending = _begin(port, 1)

    result = port.resume_reserve(pending.claim)
    accepted = [_begin(port, index) for index in range(2, 8)]

    assert result.status is CaptureReserveStatus.FAULT
    assert "readiness wait failed" in result.reason
    assert all(item.status is CaptureReserveStatus.RESERVED for item in accepted)
    assert _begin(port, 8).status is CaptureReserveStatus.CAPACITY


def test_five_second_pool_wait_is_outside_port_lock_for_other_hardware_and_cancel() -> None:
    waiting = _LifecycleFuture()
    healthy_futures = [
        _LifecycleFuture(_continuous("resident-2:generation-1")),
        _LifecycleFuture(_continuous("resident-2:generation-1")),
    ]
    first_pool = _RecoveryPool([waiting])
    second_pool = _RecoveryPool(healthy_futures)
    port, _ = _port((first_pool, second_pool))
    existing = _begin(port, 2, "coordinator-2")
    assert existing.reservation is not None
    pending = _begin(port, 1)
    resumed: list[object] = []
    resume_thread = threading.Thread(
        target=lambda: resumed.append(port.resume_reserve(pending.claim)),
    )
    resume_thread.start()
    assert waiting.wait_entered.wait(1.0)

    entered_at = time.monotonic()
    other = _begin(port, 3, "coordinator-2")
    cancel = port.cancel(existing.reservation)
    elapsed = time.monotonic() - entered_at

    assert elapsed < 0.25
    assert other.status is CaptureReserveStatus.RESERVED
    assert cancel.status is ResidentCancelStatus.CANCELLED
    assert waiting.wait_timeouts[0] is not None
    assert 4.0 < waiting.wait_timeouts[0] <= 5.0

    waiting.settle(_continuous("resident-1:generation-1"))
    resume_thread.join(1.0)
    assert not resume_thread.is_alive()
    assert resumed[0].status is CaptureReserveStatus.RESERVED
