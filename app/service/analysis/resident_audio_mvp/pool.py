"""Pool startup and bounded capture admission."""

from __future__ import annotations

import dataclasses
import math
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .contracts import (
    FLOAT32_BYTES,
    MAX_ALLOCATABLE_FRAMES,
    MAX_BOUNDED_TEXT_CHARS,
    MAX_CAPTURE_BINDINGS,
    MAX_CAPTURE_BUFFER_BYTES,
    MAX_RESIDENT_STREAMS,
    MAX_RING_BYTES_PER_STREAM,
    MAX_TIMEOUT_SECONDS,
    AudioInputBackend,
    CancelResult,
    CancelStatus,
    CaptureBinding,
    CapturedAudio,
    CaptureCompletion,
    CaptureOutcome,
    CaptureOutcomeStatus,
    BackendCloseResult,
    BackendCloseStatus,
    LifecycleOutcome,
    LifecycleStatus,
    MaintenanceReadiness,
    MaintenanceStatus,
    OpenResult,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
    ReadinessTicket,
    ShutdownReport,
    ShutdownStatus,
    StreamOpenResult,
    StreamOpenStatus,
    _validate_capture_binding,
    _issue_readiness_ticket,
    _new_lifecycle_future,
)
from .diagnostics import DiagnosticReason, DiagnosticStage, ResidentDiagnostic
from .recovery import (
    HEARTBEAT_FRESH_NS,
    MAX_ORPHANS_GLOBAL,
    MAX_ORPHANS_PER_HARDWARE,
    RECOVERY_ATTEMPT_NS,
    STALE_OBSERVATION_SECONDS,
    _OrphanRecord,
    _RecoveryState,
    retry_delay,
)
from .ring import _MappingStatus
from .stream import _ResidentStreamSlot, _fault_error, _safe_detail


def _request_error(code: ResidentErrorCode, summary: str) -> ResidentRequestError:
    return ResidentRequestError(
        ResidentError(code, summary[:MAX_BOUNDED_TEXT_CHARS])
    )


def _validate_timeout(value: object, field_name: str) -> float:
    if type(value) not in (int, float):
        raise _request_error(
            ResidentErrorCode.INVALID,
            f"{field_name} must be positive finite seconds through 300.0",
        )
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise _request_error(
            ResidentErrorCode.INVALID,
            f"{field_name} must be positive finite seconds through 300.0",
        ) from None
    if (
        not math.isfinite(normalized)
        or normalized <= 0
        or normalized > MAX_TIMEOUT_SECONDS
    ):
        raise _request_error(
            ResidentErrorCode.INVALID,
            f"{field_name} must be positive finite seconds through 300.0",
        )
    return normalized


def _validate_request_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_BOUNDED_TEXT_CHARS:
        raise _request_error(
            ResidentErrorCode.INVALID,
            f"{field_name} must be a non-empty bounded string",
        )
    return value


def _validate_specs(
    specs: object,
    *,
    max_capture_frames: object,
) -> tuple[tuple[ResidentStreamSpec, int], ...]:
    if (
        not isinstance(specs, Sequence)
        or isinstance(specs, (str, bytes, bytearray))
    ):
        raise _request_error(
            ResidentErrorCode.INVALID,
            "specs must be a sequence of ResidentStreamSpec values",
        )
    unique: dict[str, ResidentStreamSpec] = {}
    for spec in specs:
        if type(spec) is not ResidentStreamSpec:
            raise _request_error(
                ResidentErrorCode.INVALID,
                "every spec must be an exact ResidentStreamSpec",
            )
        previous = unique.get(spec.hardware_id)
        if previous is None:
            unique[spec.hardware_id] = spec
            continue
        if previous != spec:
            differing_field = next(
                field.name
                for field in dataclasses.fields(ResidentStreamSpec)
                if getattr(previous, field.name) != getattr(spec, field.name)
            )
            raise _request_error(
                ResidentErrorCode.INVALID,
                f"{spec.hardware_id} conflicts at {differing_field}",
            )
    if len(unique) > MAX_RESIDENT_STREAMS:
        raise _request_error(
            ResidentErrorCode.CAPACITY,
            "resident stream capacity is six distinct hardware ids",
        )
    if (
        type(max_capture_frames) is not int
        or max_capture_frames < 1
        or max_capture_frames > MAX_ALLOCATABLE_FRAMES
    ):
        raise _request_error(
            ResidentErrorCode.INVALID,
            "max_capture_frames must be a positive bounded integer",
        )
    layouts: list[tuple[ResidentStreamSpec, int]] = []
    for spec in sorted(unique.values(), key=lambda item: item.hardware_id):
        # Keep enough resident audio to survive UI/result projection pauses.
        # This is deliberately independent from each finite capture buffer.
        ring_frames = spec.sample_rate_hz * 10 + spec.frames_per_buffer
        if ring_frames > MAX_ALLOCATABLE_FRAMES:
            raise _request_error(
                ResidentErrorCode.INVALID,
                f"{spec.hardware_id} ring frame count exceeds the bound",
            )
        bytes_per_frame = len(spec.capture_channel_indices) * FLOAT32_BYTES
        if ring_frames > MAX_RING_BYTES_PER_STREAM // bytes_per_frame:
            raise _request_error(
                ResidentErrorCode.INVALID,
                f"{spec.hardware_id} ring allocation exceeds the byte bound",
            )
        if max_capture_frames > MAX_CAPTURE_BUFFER_BYTES // bytes_per_frame:
            raise _request_error(
                ResidentErrorCode.INVALID,
                f"{spec.hardware_id} capture allocation exceeds the byte bound",
            )
        layouts.append((spec, ring_frames))
    return tuple(layouts)


@dataclass(slots=True)
class _BindingRecord:
    binding: CaptureBinding
    publisher: _CaptureCompletionPublisher
    generation_id: str
    state: str = "RESERVED"
    buffer: np.ndarray | None = None
    trigger_monotonic_ns: int | None = None
    target_frame_count: int | None = None
    start_cursor: int | None = None
    next_cursor: int | None = None
    copied_frame_count: int = 0


@dataclass(frozen=True, slots=True, init=False, eq=False)
class _BindingTerminalProof:
    completion: CaptureCompletion
    binding: CaptureBinding
    outcome: CaptureOutcome

    def __new__(cls) -> _BindingTerminalProof:
        raise TypeError("terminal proof is created only by its active publisher")


class _CaptureCompletionPublisher:
    __slots__ = ("_binding", "_completion", "_prepared")

    def __init__(
        self,
        binding: CaptureBinding,
        completion: CaptureCompletion,
    ) -> None:
        self._binding = binding
        self._completion = completion
        self._prepared = False

    def prepare(self, outcome: CaptureOutcome) -> None:
        if self._prepared or self._completion._stored_outcome() is not None:
            raise RuntimeError("completion already has an outcome")
        if outcome.binding is not self._binding:
            raise RuntimeError("outcome does not match publisher binding")
        proof = object.__new__(_BindingTerminalProof)
        object.__setattr__(proof, "completion", self._completion)
        object.__setattr__(proof, "binding", self._binding)
        object.__setattr__(proof, "outcome", outcome)
        self._completion._outcome = outcome
        self._completion._terminal_proof = proof
        self._prepared = True

    def publish(self) -> None:
        if not self._prepared:
            raise RuntimeError("completion has no outcome")
        self._completion._event.set()


class ResidentAudioPool:
    """Thin process-local owner of resident input streams."""

    def __init__(
        self,
        *,
        specs: tuple[ResidentStreamSpec, ...],
        backend: AudioInputBackend,
        max_capture_frames: int,
        startup_stream_timeout_s: float,
        fault_close_timeout_s: float,
        shutdown_timeout_s: float,
        endpoint_identities: Mapping[str, object] | None = None,
        endpoint_resolver: object | None = None,
        drain_observer: Callable[
            [str, np.ndarray, int, int, int], None
        ]
        | None = None,
        monotonic_ns: Callable[[], int],
    ) -> None:
        self._lock = threading.Lock()
        self._state = "OPENING"
        self._backend = backend
        self._specs = {spec.hardware_id: spec for spec in specs}
        self._slots: dict[str, _ResidentStreamSlot] = {}
        self._max_capture_frames = max_capture_frames
        self._startup_stream_timeout_s = startup_stream_timeout_s
        self._fault_close_timeout_s = fault_close_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._endpoint_identities = dict(endpoint_identities or {})
        self._endpoint_resolver = endpoint_resolver
        self._drain_observer = drain_observer
        self._monotonic_ns = monotonic_ns
        self._generation_numbers = {spec.hardware_id: 1 for spec in specs}
        self._generation_ids: dict[str, str] = {}
        self._recovery_states = {
            spec.hardware_id: _RecoveryState(desired_revision=1)
            for spec in specs
        }
        self._orphans: list[_OrphanRecord] = []
        self._retired_drains: dict[str, _ResidentStreamSlot] = {}
        self._next_readiness_number = 1
        self._owner_nonce = object()
        self._next_binding_number = 1
        self._bindings: dict[str, _BindingRecord] = {}
        self._run_ids: dict[str, str] = {}
        self._capture_permits = 0
        self._resource_errors: list[ResidentError] = []
        self._shutdown_gate = threading.Lock()
        self._shutdown_owner = False
        self._shutdown_started_ns: int | None = None
        self._shutdown_deadline_ns: int | None = None
        self._shutdown_report: ShutdownReport | None = None

    @classmethod
    def open(
        cls,
        specs: Sequence[ResidentStreamSpec],
        *,
        backend: AudioInputBackend,
        max_capture_frames: int,
        startup_stream_timeout_s: float,
        fault_close_timeout_s: float,
        shutdown_timeout_s: float,
        endpoint_identities: Mapping[str, object] | None = None,
        endpoint_resolver: object | None = None,
        drain_observer: Callable[
            [str, np.ndarray, int, int, int], None
        ]
        | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> OpenResult:
        layouts = _validate_specs(
            specs,
            max_capture_frames=max_capture_frames,
        )
        startup_timeout = _validate_timeout(
            startup_stream_timeout_s,
            "startup_stream_timeout_s",
        )
        fault_timeout = _validate_timeout(
            fault_close_timeout_s,
            "fault_close_timeout_s",
        )
        shutdown_timeout = _validate_timeout(
            shutdown_timeout_s,
            "shutdown_timeout_s",
        )
        if not callable(getattr(backend, "create_input", None)):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "backend must provide create_input",
            )
        selected_monotonic_ns = (
            time.monotonic_ns if monotonic_ns is None else monotonic_ns
        )
        if not callable(selected_monotonic_ns):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "monotonic_ns must be callable",
            )
        identities = dict(endpoint_identities or {})
        if identities and set(identities) != {spec.hardware_id for spec, _ in layouts}:
            raise _request_error(
                ResidentErrorCode.INVALID,
                "endpoint identities must exactly match resident specs",
            )
        if endpoint_resolver is not None and not callable(
            getattr(endpoint_resolver, "resolve", None)
        ):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "endpoint_resolver must provide resolve",
            )
        if drain_observer is not None and not callable(drain_observer):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "drain_observer must be callable or None",
            )
        pool = cls(
            specs=tuple(spec for spec, _ in layouts),
            backend=backend,
            max_capture_frames=max_capture_frames,
            startup_stream_timeout_s=startup_timeout,
            fault_close_timeout_s=fault_timeout,
            shutdown_timeout_s=shutdown_timeout,
            endpoint_identities=identities,
            endpoint_resolver=endpoint_resolver,
            drain_observer=drain_observer,
            monotonic_ns=selected_monotonic_ns,
        )
        reports: list[StreamOpenResult] = []
        for spec, ring_frames in layouts:
            report = pool._open_slot(spec, ring_frames)
            reports.append(report)
        pool._state = "OPEN"
        with pool._lock:
            for report in reports:
                if report.status is StreamOpenStatus.READY:
                    pool._generation_ids[report.hardware_id] = (
                        f"{report.hardware_id}:generation-1"
                    )
        return OpenResult(
            pool=pool,
            streams=tuple(reports),
            ready_count=sum(
                report.status is StreamOpenStatus.READY for report in reports
            ),
        )

    def _bound_drain_observer(
        self,
        spec: ResidentStreamSpec,
    ) -> Callable[[np.ndarray, int, int], None] | None:
        observer = self._drain_observer
        if observer is None:
            return None

        def observe(
            samples: np.ndarray,
            frame_count: int,
            callback_monotonic_ns: int,
        ) -> None:
            observer(
                spec.hardware_id,
                samples,
                frame_count,
                callback_monotonic_ns,
                spec.sample_rate_hz,
            )

        return observe

    def _open_slot(
        self,
        spec: ResidentStreamSpec,
        ring_frames: int,
    ) -> StreamOpenResult:
        try:
            slot = _ResidentStreamSlot(
                spec,
                ring_capacity_frames=ring_frames,
                monotonic_ns=self._monotonic_ns,
                decision_lock=self._lock,
            )
        except Exception as exc:
            return StreamOpenResult(
                spec.hardware_id,
                StreamOpenStatus.FAULTED,
                _fault_error(
                    phase="ring allocation",
                    hardware_id=spec.hardware_id,
                    detail=f"{type(exc).__name__}: {_safe_detail(exc)}",
                ),
            )
        self._slots[spec.hardware_id] = slot
        callback, on_fault = slot.backend_entrypoints()
        try:
            handle = self._backend.create_input(
                spec,
                callback,
                on_fault,
            )
        except Exception as exc:
            error = _fault_error(
                phase="create input",
                hardware_id=spec.hardware_id,
                detail=f"{type(exc).__name__}: {_safe_detail(exc)}",
            )
            slot.latch_owner_fault(error)
            return StreamOpenResult(
                spec.hardware_id,
                StreamOpenStatus.FAULTED,
                error,
            )
        slot._handle = handle
        slot.publish_drain_owner(
            self._drain_stream,
            self._bound_drain_observer(spec),
        )
        deadline_ns = self._monotonic_ns() + int(
            self._startup_stream_timeout_s * 1_000_000_000
        )
        start_error: ResidentError | None = None
        try:
            handle.start(deadline_ns)
        except Exception as exc:
            start_error = _fault_error(
                phase="start input",
                hardware_id=spec.hardware_id,
                detail=f"{type(exc).__name__}: {_safe_detail(exc)}",
            )
            slot.latch_owner_fault(start_error)
        if start_error is None and not slot._first_block_event.is_set():
            remaining_ns = max(0, deadline_ns - self._monotonic_ns())
            if remaining_ns:
                slot._first_block_event.wait(remaining_ns / 1_000_000_000)
        owner_monotonic_ns = self._monotonic_ns()
        with self._lock:
            ready, error = slot.finish_start(deadline_ns, owner_monotonic_ns)
            startup_handle, startup_close_claimed = (
                slot.resolve_startup_close_under_decision_lock(
                    failed=not ready,
                )
            )
        if ready:
            return StreamOpenResult(
                spec.hardware_id,
                StreamOpenStatus.READY,
            )
        fault_ns = (
            slot._fault_monotonic_ns
            if slot._fault_monotonic_ns is not None
            else self._monotonic_ns()
        )
        self._cleanup_failed_start(
            slot,
            fault_ns,
            handle=startup_handle,
            claimed=startup_close_claimed,
        )
        return StreamOpenResult(
            spec.hardware_id,
            StreamOpenStatus.FAULTED,
            error,
        )

    def _cleanup_failed_start(
        self,
        slot: _ResidentStreamSlot,
        fault_monotonic_ns: int,
        *,
        handle: object,
        claimed: bool,
    ) -> None:
        if not claimed:
            return
        hardware_id = slot._spec.hardware_id
        try:
            handle.request_stop()
        except Exception as exc:
            self._record_resource_error(
                "startup request_stop",
                hardware_id,
                exc,
            )
        deadline_ns = fault_monotonic_ns + int(
            self._shutdown_timeout_s * 1_000_000_000
        )
        try:
            close_result = handle.close(deadline_ns)
        except Exception as exc:
            self._record_resource_error("startup close", hardware_id, exc)
            close_result = BackendCloseResult(
                BackendCloseStatus.NOT_CONFIRMED,
                ResidentError(
                    ResidentErrorCode.NON_CLEAN,
                    (
                        f"startup close {hardware_id} "
                        f"{type(exc).__name__}: {_safe_detail(exc)}"
                    )[:MAX_BOUNDED_TEXT_CHARS],
                ),
            )
        else:
            if type(close_result) is not BackendCloseResult:
                invalid_result = TypeError(
                    "backend close returned an invalid result"
                )
                self._record_resource_error(
                    "startup close result",
                    hardware_id,
                    invalid_result,
                )
                close_result = BackendCloseResult(
                    BackendCloseStatus.NOT_CONFIRMED,
                    ResidentError(
                        ResidentErrorCode.NON_CLEAN,
                        f"startup close result {hardware_id} TypeError",
                    ),
                )
        slot.record_close_result_from_control_owner(close_result)

    def begin_readiness(
        self,
        hardware_id: str,
        deadline_monotonic_ns: int,
    ) -> ReadinessTicket:
        normalized_hardware_id = _validate_request_text(
            hardware_id, "hardware_id"
        )
        now_ns = self._monotonic_ns()
        if (
            type(deadline_monotonic_ns) is not int
            or deadline_monotonic_ns <= now_ns
            or deadline_monotonic_ns > (1 << 63) - 1
        ):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "deadline_monotonic_ns must be a future positive integer",
            )
        lifecycle_deadline_ns = min(
            deadline_monotonic_ns,
            now_ns + RECOVERY_ATTEMPT_NS,
        )
        start_worker = False
        with self._lock:
            if normalized_hardware_id not in self._specs:
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    f"unknown hardware_id {normalized_hardware_id}",
                )
            if self._state != "OPEN":
                future, publisher = _new_lifecycle_future()
                publisher.settle(
                    LifecycleOutcome(
                        LifecycleStatus.LIFECYCLE_CANCELLED,
                        diagnostic=self._diagnostic(
                            DiagnosticStage.LIFECYCLE,
                            DiagnosticReason.CLOSING,
                            "resident pool is closing",
                        ),
                    )
                )
            else:
                slot = self._slots.get(normalized_hardware_id)
                if slot is not None and slot.ready:
                    fault, _ = slot.fault_details_under_decision_lock()
                    heartbeat_ns = slot.callback_heartbeat_monotonic_ns
                    if (
                        fault is None
                        and heartbeat_ns is not None
                        and now_ns - heartbeat_ns <= HEARTBEAT_FRESH_NS
                    ):
                        future, publisher = _new_lifecycle_future()
                        publisher.settle(
                            LifecycleOutcome(
                                LifecycleStatus.GENERATION_CONTINUOUS,
                                generation_id=self._generation_ids[
                                    normalized_hardware_id
                                ],
                            )
                        )
                    else:
                        future, start_worker = self._ensure_recovery_locked(
                            normalized_hardware_id,
                            lifecycle_deadline_ns,
                        )
                else:
                    future, start_worker = self._ensure_recovery_locked(
                        normalized_hardware_id,
                        lifecycle_deadline_ns,
                    )
            ticket_number = self._next_readiness_number
            self._next_readiness_number += 1
            ticket = _issue_readiness_ticket(
                ticket_id=f"readiness-{ticket_number:08d}",
                hardware_id=normalized_hardware_id,
                future=future,
                owner_nonce=self._owner_nonce,
            )
        if start_worker:
            self._start_recovery_worker(normalized_hardware_id)
        return ticket

    def _ensure_recovery_locked(
        self,
        hardware_id: str,
        deadline_monotonic_ns: int,
    ) -> tuple[object, bool]:
        state = self._recovery_states[hardware_id]
        worker = state.worker
        if worker is not None or state.starting:
            if (
                state.future is not None
                and state.future.result_if_ready() is None
            ):
                return state.future, False
            future, publisher = _new_lifecycle_future()
            state.future = future
            state.publisher = publisher
            state.deadline_monotonic_ns = deadline_monotonic_ns
            state.wake.set()
            return future, False
        future, publisher = _new_lifecycle_future()
        state.future = future
        state.publisher = publisher
        state.deadline_monotonic_ns = deadline_monotonic_ns
        state.starting = True
        return future, True

    def _start_recovery_worker(self, hardware_id: str) -> None:
        with self._lock:
            state = self._recovery_states[hardware_id]
            if self._state != "OPEN" or state.stop.is_set():
                state.starting = False
                return
            if state.worker is not None:
                state.starting = False
                return
            if not state.starting:
                return
            worker = threading.Thread(
                name=f"resident-recovery-{hardware_id}",
                target=self._recovery_worker,
                args=(hardware_id,),
                daemon=True,
            )
            state.worker = worker
            try:
                worker.start()
            except Exception as exc:
                if state.worker is worker:
                    state.worker = None
                state.starting = False
                self._settle_recovery_locked(
                    hardware_id,
                    LifecycleOutcome(
                        LifecycleStatus.RECOVERY_FAILED,
                        diagnostic=self._diagnostic(
                            DiagnosticStage.INTERNAL,
                            DiagnosticReason.INVARIANT_VIOLATION,
                            "recovery worker start "
                            f"{type(exc).__name__}: {_safe_detail(exc)}",
                        ),
                    ),
                )
                return
            state.starting = False

    def _request_runtime_recovery(self, hardware_id: str) -> None:
        now_ns = self._monotonic_ns()
        with self._lock:
            if self._state != "OPEN":
                return
            _future, start_worker = self._ensure_recovery_locked(
                hardware_id,
                min((1 << 63) - 1, now_ns + RECOVERY_ATTEMPT_NS),
            )
        if start_worker:
            self._start_recovery_worker(hardware_id)

    def _recovery_worker(self, hardware_id: str) -> None:
        while True:
            with self._lock:
                state = self._recovery_states[hardware_id]
                if self._state != "OPEN" or state.stop.is_set():
                    self._settle_recovery_locked(
                        hardware_id,
                        LifecycleOutcome(
                            LifecycleStatus.LIFECYCLE_CANCELLED,
                            diagnostic=self._diagnostic(
                                DiagnosticStage.LIFECYCLE,
                                DiagnosticReason.CLOSING,
                                "resident recovery was cancelled by shutdown",
                            ),
                        ),
                    )
                    state.worker = None
                    return
                slot = self._slots.get(hardware_id)
                deadline_ns = state.deadline_monotonic_ns or min(
                    (1 << 63) - 1,
                    self._monotonic_ns() + RECOVERY_ATTEMPT_NS,
                )
                observed_sequence = None if slot is None else slot.callback_sequence
                observed_fault = None
                if slot is not None:
                    observed_fault, _ = slot.fault_details_under_decision_lock()

            if slot is not None and observed_fault is None:
                observation_started_ns = self._monotonic_ns()
                suspect_deadline_ns = min(
                    deadline_ns,
                    observation_started_ns
                    + int(STALE_OBSERVATION_SECONDS * 1_000_000_000),
                )
                remaining_s = max(
                    0.0,
                    (suspect_deadline_ns - self._monotonic_ns())
                    / 1_000_000_000,
                )
                progressed = slot.wait_for_callback_after(
                    observed_sequence,
                    remaining_s,
                )
                continuity_proven = False
                if progressed:
                    remaining_s = max(
                        0.0,
                        (suspect_deadline_ns - self._monotonic_ns())
                        / 1_000_000_000,
                    )
                    continuity_proven = slot.wait_for_drain_after(
                        observed_sequence,
                        remaining_s,
                    )
                with self._lock:
                    fault, _ = slot.fault_details_under_decision_lock()
                    if (
                        continuity_proven
                        and fault is None
                        and self._slots.get(hardware_id) is slot
                        and slot.ready
                    ):
                        self._settle_recovery_locked(
                            hardware_id,
                            LifecycleOutcome(
                                LifecycleStatus.GENERATION_CONTINUOUS,
                                generation_id=self._generation_ids[hardware_id],
                            ),
                        )
                        self._recovery_states[hardware_id].worker = None
                        return

            try:
                detached = self._detach_generation_for_recovery(
                    hardware_id, slot
                )
                if detached is not None:
                    self._retire_slot(detached, deadline_ns)
                if self._monotonic_ns() >= deadline_ns:
                    if detached is not None:
                        stage = DiagnosticStage.RETIRE_OLD
                        reason = DiagnosticReason.CLOSE_TIMEOUT
                        detail = (
                            "old generation retirement exhausted the "
                            "recovery attempt budget"
                        )
                    else:
                        stage = DiagnosticStage.BACKGROUND_RETRY
                        reason = DiagnosticReason.RETRY_SCHEDULED
                        detail = (
                            "recovery attempt budget expired before "
                            "candidate resolution"
                        )
                    outcome = LifecycleOutcome(
                        LifecycleStatus.RECOVERY_PENDING,
                        diagnostic=self._diagnostic(stage, reason, detail),
                    )
                else:
                    outcome = self._open_replacement(
                        hardware_id,
                        deadline_ns,
                    )
            except Exception as exc:
                outcome = LifecycleOutcome(
                    LifecycleStatus.RECOVERY_FAILED,
                    diagnostic=self._diagnostic(
                        DiagnosticStage.INTERNAL,
                        DiagnosticReason.INVARIANT_VIOLATION,
                        f"recovery invariant {type(exc).__name__}: "
                        f"{_safe_detail(exc)}",
                    ),
                )
            with self._lock:
                self._settle_recovery_locked(hardware_id, outcome)
                state = self._recovery_states[hardware_id]
                if outcome.status is LifecycleStatus.LIFECYCLE_CANCELLED:
                    state.worker = None
                    return
                if outcome.status is LifecycleStatus.GENERATION_REPLACED:
                    state.attempt = 0
                    has_orphans = any(
                        item.hardware_id == hardware_id for item in self._orphans
                    )
                    if not has_orphans:
                        state.worker = None
                        return
                    cleanup_only = True
                    delay_s = retry_delay(0)
                else:
                    cleanup_only = False
                    state.attempt += 1
                    future, publisher = _new_lifecycle_future()
                    state.future = future
                    state.publisher = publisher
                    state.deadline_monotonic_ns = None
                    delay_s = retry_delay(max(0, state.attempt - 1))

            if cleanup_only:
                cleanup_attempt = 0
                while True:
                    state.wake.wait(retry_delay(cleanup_attempt))
                    state.wake.clear()
                    self._cleanup_orphans_once(hardware_id)
                    with self._lock:
                        if self._state != "OPEN" or state.stop.is_set():
                            state.worker = None
                            return
                        if (
                            state.future is not None
                            and state.future.result_if_ready() is None
                        ):
                            break
                        if not any(
                            item.hardware_id == hardware_id
                            for item in self._orphans
                        ):
                            state.worker = None
                            return
                    cleanup_attempt += 1
                continue

            state.wake.wait(delay_s)
            state.wake.clear()
            self._cleanup_orphans_once(hardware_id)
            with self._lock:
                state = self._recovery_states[hardware_id]
                if self._state == "OPEN" and not state.stop.is_set():
                    state.deadline_monotonic_ns = min(
                        (1 << 63) - 1,
                        self._monotonic_ns() + RECOVERY_ATTEMPT_NS,
                    )

    def _detach_generation_for_recovery(
        self,
        hardware_id: str,
        expected_slot: _ResidentStreamSlot | None,
    ) -> _ResidentStreamSlot | None:
        with self._lock:
            slot = self._slots.get(hardware_id)
            if slot is None or (expected_slot is not None and slot is not expected_slot):
                return None
            slot.invalidate_generation_under_decision_lock("RETIRED")
            del self._slots[hardware_id]
            self._generation_ids.pop(hardware_id, None)
            close_result = slot.close_result_snapshot()
            if (
                type(close_result) is BackendCloseResult
                and close_result.status is not BackendCloseStatus.CLOSED
                and slot._handle is not None
            ):
                self._register_orphan_locked(
                    slot,
                    slot._handle,
                    close_result.error,
                )
            fault, _ = slot.fault_details_under_decision_lock()
            terminal_error = fault or _fault_error(
                phase="liveness",
                hardware_id=hardware_id,
                detail="heartbeat_stale",
            )
            for binding_id in tuple(self._bindings):
                record = self._bindings.get(binding_id)
                if record is not None and record.binding.hardware_id == hardware_id:
                    self._commit_terminal_locked(
                        record,
                        CaptureOutcomeStatus.FAILED,
                        terminal_error,
                    )
            return slot

    def _retire_slot(
        self,
        slot: _ResidentStreamSlot,
        deadline_ns: int,
        *,
        preclaimed: tuple[object, bool] | None = None,
    ) -> None:
        slot.stop_worker()
        if preclaimed is None:
            with self._lock:
                handle, claimed = slot.claim_close_attempt_under_decision_lock()
        else:
            handle, claimed = preclaimed
        if claimed:
            try:
                handle.request_stop()
            except Exception as exc:
                self._record_resource_error(
                    "retire request_stop", slot._spec.hardware_id, exc
                )
            try:
                result = handle.close(deadline_ns)
            except Exception as exc:
                result = BackendCloseResult(
                    BackendCloseStatus.NOT_CONFIRMED,
                    ResidentError(
                        ResidentErrorCode.NON_CLEAN,
                        (
                            f"retire close {slot._spec.hardware_id} "
                            f"{type(exc).__name__}: {_safe_detail(exc)}"
                        )[:MAX_BOUNDED_TEXT_CHARS],
                    ),
                )
            if type(result) is not BackendCloseResult:
                result = BackendCloseResult(
                    BackendCloseStatus.NOT_CONFIRMED,
                    ResidentError(
                        ResidentErrorCode.NON_CLEAN,
                        f"retire close {slot._spec.hardware_id} invalid result",
                    ),
                )
            slot.record_close_result_from_control_owner(result)
            if result.status is not BackendCloseStatus.CLOSED:
                with self._lock:
                    self._register_orphan_locked(
                        slot,
                        handle,
                        result.error,
                    )
        remaining_s = max(
            0.0,
            (deadline_ns - self._monotonic_ns()) / 1_000_000_000,
        )
        if not slot.join_worker(remaining_s):
            worker_id = slot.worker_id()
            if worker_id is not None:
                with self._lock:
                    self._retired_drains[worker_id] = slot

    def _open_replacement(
        self,
        hardware_id: str,
        deadline_ns: int,
    ) -> LifecycleOutcome:
        with self._lock:
            per_hardware = sum(
                item.hardware_id == hardware_id for item in self._orphans
            )
            if (
                per_hardware >= MAX_ORPHANS_PER_HARDWARE
                or len(self._orphans) >= MAX_ORPHANS_GLOBAL
            ):
                return LifecycleOutcome(
                    LifecycleStatus.RECOVERY_FAILED,
                    diagnostic=self._diagnostic(
                        DiagnosticStage.BACKGROUND_RETRY,
                        DiagnosticReason.ORPHAN_LIMIT_REACHED,
                        "unresolved native cleanup reached its bounded limit",
                    ),
                )
            desired_revision = self._recovery_states[hardware_id].desired_revision
            frozen = self._endpoint_identities.get(hardware_id)
            original_spec = self._specs[hardware_id]
        if frozen is None or self._endpoint_resolver is None:
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.RESOLVE_ENDPOINT,
                    DiagnosticReason.ENDPOINT_SCAN_FAILED,
                    "no exact recovery endpoint resolver is configured",
                ),
            )
        try:
            resolve_until = getattr(
                self._endpoint_resolver,
                "resolve_until",
                None,
            )
            if callable(resolve_until):
                resolved = resolve_until(frozen, deadline_ns)
            else:
                resolved = self._endpoint_resolver.resolve(frozen)
        except Exception as exc:
            reason = getattr(
                exc,
                "reason_code",
                DiagnosticReason.ENDPOINT_SCAN_FAILED,
            )
            if reason not in (
                DiagnosticReason.ENDPOINT_SCAN_FAILED,
                DiagnosticReason.ENDPOINT_MISSING,
                DiagnosticReason.ENDPOINT_AMBIGUOUS,
                DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
            ):
                reason = DiagnosticReason.ENDPOINT_SCAN_FAILED
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_PENDING
                if getattr(exc, "is_timeout", False) is True
                else LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.RESOLVE_ENDPOINT,
                    reason,
                    f"endpoint resolution {type(exc).__name__}: {_safe_detail(exc)}",
                ),
            )
        frozen_uid = getattr(frozen, "stable_backend_uid", None)
        if (
            getattr(resolved, "hardware_id", None)
            != getattr(frozen, "hardware_id", None)
            or getattr(resolved, "scan_id", None)
            == getattr(frozen, "scan_id", None)
            or (
                frozen_uid is not None
                and getattr(resolved, "stable_backend_uid", None) != frozen_uid
            )
        ):
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.RESOLVE_ENDPOINT,
                    DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
                    "resolved endpoint does not preserve the frozen exact identity",
                ),
            )
        if (
            getattr(resolved, "device_name", None) != original_spec.device_name
            or getattr(resolved, "hostapi_name", None) != original_spec.hostapi_name
            or getattr(resolved, "sample_rate_hz", None)
            != float(original_spec.sample_rate_hz)
            or getattr(resolved, "input_channels", None)
            != original_spec.input_channel_count
        ):
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.VALIDATE_CANDIDATE,
                    DiagnosticReason.FORMAT_MISMATCH,
                    "resolved endpoint is incompatible with the frozen stream format",
                ),
            )
        try:
            candidate_spec = dataclasses.replace(
                original_spec,
                device_index=resolved.device_index,
                hostapi_index=resolved.hostapi_index,
            )
        except Exception as exc:
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.VALIDATE_CANDIDATE,
                    DiagnosticReason.CAPABILITY_MISMATCH,
                    f"candidate coordinates {type(exc).__name__}: {_safe_detail(exc)}",
                ),
            )
        if self._monotonic_ns() >= deadline_ns:
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_PENDING,
                diagnostic=self._diagnostic(
                    DiagnosticStage.OPEN_CANDIDATE,
                    DiagnosticReason.OPEN_TIMEOUT,
                    "candidate open budget expired after endpoint resolution",
                ),
            )
        ring_frames = (
            candidate_spec.sample_rate_hz * 10
            + candidate_spec.frames_per_buffer
        )
        try:
            candidate = _ResidentStreamSlot(
                candidate_spec,
                ring_capacity_frames=ring_frames,
                monotonic_ns=self._monotonic_ns,
                decision_lock=self._lock,
            )
        except Exception as exc:
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.OPEN_CANDIDATE,
                    DiagnosticReason.OPEN_FAILED,
                    f"candidate allocation {type(exc).__name__}: {_safe_detail(exc)}",
                ),
            )
        callback, on_fault = candidate.backend_entrypoints()
        try:
            handle = self._backend.create_input(candidate_spec, callback, on_fault)
        except Exception as exc:
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.OPEN_CANDIDATE,
                    DiagnosticReason.OPEN_FAILED,
                    f"candidate open {type(exc).__name__}: {_safe_detail(exc)}",
                ),
            )
        candidate._handle = handle
        candidate.publish_drain_owner(
            self._drain_stream,
            self._bound_drain_observer(candidate_spec),
        )
        try:
            handle.start(deadline_ns)
        except Exception as exc:
            failed_stage = getattr(handle, "start_failure_stage", None)
            is_open = failed_stage == "open"
            deadline_expired = self._monotonic_ns() >= deadline_ns
            stage = (
                DiagnosticStage.OPEN_CANDIDATE
                if is_open
                else DiagnosticStage.START_CANDIDATE
            )
            reason = (
                DiagnosticReason.OPEN_TIMEOUT
                if is_open
                and (isinstance(exc, TimeoutError) or deadline_expired)
                else DiagnosticReason.OPEN_FAILED
                if is_open
                else DiagnosticReason.START_TIMEOUT
                if isinstance(exc, TimeoutError) or deadline_expired
                else DiagnosticReason.START_FAILED
            )
            self._retire_unpublished_candidate(candidate, deadline_ns)
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_PENDING
                if reason in (
                    DiagnosticReason.OPEN_TIMEOUT,
                    DiagnosticReason.START_TIMEOUT,
                )
                else LifecycleStatus.RECOVERY_FAILED,
                diagnostic=self._diagnostic(
                    stage,
                    reason,
                    f"candidate {failed_stage or 'start'} "
                    f"{type(exc).__name__}: {_safe_detail(exc)}",
                ),
            )
        if not candidate._first_block_event.is_set():
            remaining_s = max(
                0.0,
                (deadline_ns - self._monotonic_ns()) / 1_000_000_000,
            )
            candidate._first_block_event.wait(remaining_s)
        owner_now_ns = self._monotonic_ns()
        with self._lock:
            ready, _error = candidate.finish_start(deadline_ns, owner_now_ns)
            if ready:
                candidate.resolve_startup_close_under_decision_lock(
                    failed=False
                )
        if not ready:
            candidate_fault, _fault_ns = (
                candidate.fault_details_under_decision_lock()
            )
            self._retire_unpublished_candidate(candidate, deadline_ns)
            if (
                candidate_fault is not None
                and "first block" not in candidate_fault.summary
            ):
                reason = self._liveness_reason(candidate_fault)
                return LifecycleOutcome(
                    LifecycleStatus.RECOVERY_FAILED,
                    diagnostic=self._diagnostic(
                        DiagnosticStage.LIVENESS,
                        reason,
                        candidate_fault.summary,
                    ),
                )
            return LifecycleOutcome(
                LifecycleStatus.RECOVERY_PENDING,
                diagnostic=self._diagnostic(
                    DiagnosticStage.AWAIT_FIRST_SAMPLE,
                    DiagnosticReason.FIRST_SAMPLE_TIMEOUT,
                    "audio input device did not provide its first accepted sample",
                ),
            )
        retirement_deadline_ns = deadline_ns
        with self._lock:
            state = self._recovery_states[hardware_id]
            if (
                self._state != "OPEN"
                or state.desired_revision != desired_revision
                or hardware_id in self._slots
            ):
                publish = False
                if (
                    self._state != "OPEN"
                    and self._shutdown_deadline_ns is not None
                ):
                    retirement_deadline_ns = min(
                        retirement_deadline_ns,
                        self._shutdown_deadline_ns,
                    )
            else:
                generation_number = self._generation_numbers[hardware_id] + 1
                self._generation_numbers[hardware_id] = generation_number
                generation_id = f"{hardware_id}:generation-{generation_number}"
                self._slots[hardware_id] = candidate
                self._specs[hardware_id] = candidate_spec
                self._generation_ids[hardware_id] = generation_id
                publish = True
        if not publish:
            self._retire_unpublished_candidate(
                candidate,
                retirement_deadline_ns,
            )
            return LifecycleOutcome(
                LifecycleStatus.LIFECYCLE_CANCELLED,
                diagnostic=self._diagnostic(
                    DiagnosticStage.PUBLISH_CANDIDATE,
                    DiagnosticReason.CANDIDATE_SUPERSEDED,
                    "candidate was superseded before publication",
                ),
            )
        return LifecycleOutcome(
            LifecycleStatus.GENERATION_REPLACED,
            generation_id=generation_id,
            diagnostic=self._diagnostic(
                DiagnosticStage.PUBLISH_CANDIDATE,
                DiagnosticReason.GENERATION_REPLACED_FOR_TRIGGER,
                "generation_replaced_for_trigger",
            ),
        )

    def _retire_unpublished_candidate(
        self,
        candidate: _ResidentStreamSlot,
        deadline_ns: int,
    ) -> None:
        with self._lock:
            handle, claimed = candidate.resolve_startup_close_under_decision_lock(
                failed=True
            )
        self._retire_slot(
            candidate,
            deadline_ns,
            preclaimed=(handle, claimed),
        )

    def _cleanup_orphans_once(self, hardware_id: str) -> None:
        with self._lock:
            records = tuple(
                item for item in self._orphans if item.hardware_id == hardware_id
            )
        for record in records:
            state_value = getattr(
                getattr(record.handle, "lifecycle_state", None),
                "value",
                None,
            )
            if state_value == "CALL_TIMED_OUT":
                waiter = getattr(record.handle, "wait_for_lifecycle_settle", None)
                if callable(waiter):
                    waiter(0.0)
                if (
                    getattr(
                        getattr(record.handle, "lifecycle_state", None),
                        "value",
                        None,
                    )
                    == "CALL_TIMED_OUT"
                ):
                    continue
            try:
                record.handle.request_stop()
                result = record.handle.close(
                    min(
                        (1 << 63) - 1,
                        self._monotonic_ns()
                        + int(self._fault_close_timeout_s * 1_000_000_000),
                    )
                )
            except Exception as exc:
                record.last_error = ResidentError(
                    ResidentErrorCode.NON_CLEAN,
                    (
                        f"orphan cleanup {hardware_id} "
                        f"{type(exc).__name__}: {_safe_detail(exc)}"
                    )[:MAX_BOUNDED_TEXT_CHARS],
                )
                record.cleanup_attempt += 1
                continue
            if (
                type(result) is BackendCloseResult
                and result.status is BackendCloseStatus.CLOSED
            ):
                with self._lock:
                    if record in self._orphans:
                        self._release_orphan_locked(record)
            elif type(result) is BackendCloseResult:
                record.last_error = result.error
                record.cleanup_attempt += 1
            else:
                record.last_error = ResidentError(
                    ResidentErrorCode.NON_CLEAN,
                    f"orphan cleanup {hardware_id} returned an invalid result",
                )
                record.cleanup_attempt += 1

    def _release_orphan_locked(self, record: _OrphanRecord) -> None:
        self._orphans.remove(record)
        waiting_hardware = sorted(
            self._recovery_states,
            key=lambda item: (item in self._slots, item),
        )
        for waiting_id in waiting_hardware:
            recovery = self._recovery_states[waiting_id]
            if recovery.worker is not None:
                recovery.wake.set()
                break

    def _register_orphan_locked(
        self,
        slot: _ResidentStreamSlot,
        handle: object,
        last_error: ResidentError | None,
    ) -> None:
        if any(item.handle is handle for item in self._orphans):
            return
        self._orphans.append(
            _OrphanRecord(
                slot._spec.hardware_id,
                slot,
                handle,
                last_error=last_error,
            )
        )

    @staticmethod
    def _liveness_reason(error: ResidentError) -> DiagnosticReason:
        summary = error.summary.lower()
        if "ingress_overflow" in summary:
            return DiagnosticReason.INGRESS_OVERFLOW
        if "input_overflow" in summary:
            return DiagnosticReason.PORTAUDIO_INPUT_OVERFLOW
        if "backend fault" in summary or "device_fault" in summary:
            return DiagnosticReason.BACKEND_FAULT
        return DiagnosticReason.CALLBACK_CONTINUITY_LOST

    def _settle_recovery_locked(
        self,
        hardware_id: str,
        outcome: LifecycleOutcome,
    ) -> None:
        publisher = self._recovery_states[hardware_id].publisher
        if publisher is not None:
            publisher.settle(outcome)

    @staticmethod
    def _diagnostic(
        stage: DiagnosticStage,
        reason: DiagnosticReason,
        detail: str,
    ) -> ResidentDiagnostic:
        bounded = detail[:MAX_BOUNDED_TEXT_CHARS] or reason.value
        return ResidentDiagnostic(stage, reason, bounded)

    def orphan_counts(self) -> tuple[int, dict[str, int]]:
        with self._lock:
            by_hardware = {
                hardware_id: sum(
                    item.hardware_id == hardware_id for item in self._orphans
                )
                for hardware_id in sorted(self._specs)
                if any(item.hardware_id == hardware_id for item in self._orphans)
            }
            return len(self._orphans), by_hardware

    def reserve_capture(
        self,
        run_id: str,
        hardware_id: str,
        *,
        generation_id: str | None = None,
    ) -> CaptureBinding:
        normalized_run_id = _validate_request_text(run_id, "run_id")
        normalized_hardware_id = _validate_request_text(
            hardware_id,
            "hardware_id",
        )
        normalized_generation_id = (
            None
            if generation_id is None
            else _validate_request_text(generation_id, "generation_id")
        )
        with self._lock:
            if self._state != "OPEN":
                raise _request_error(
                    ResidentErrorCode.CLOSING,
                    "resident pool is closing",
                )
            if normalized_run_id in self._run_ids:
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    f"run_id {normalized_run_id} already has a binding",
                )
            slot = self._slots.get(normalized_hardware_id)
            if slot is None or not slot.ready:
                raise _request_error(
                    ResidentErrorCode.NOT_READY,
                    f"hardware {normalized_hardware_id} is not ready",
                )
            current_generation_id = self._generation_ids.get(
                normalized_hardware_id
            )
            if (
                current_generation_id is None
                or (
                    normalized_generation_id is not None
                    and normalized_generation_id != current_generation_id
                )
            ):
                raise _request_error(
                    ResidentErrorCode.NOT_READY,
                    f"hardware {normalized_hardware_id} generation changed",
                )
            if self._capture_permits >= MAX_CAPTURE_BINDINGS:
                raise _request_error(
                    ResidentErrorCode.CAPACITY,
                    "resident capture capacity is six non-terminal bindings",
                )
            binding_id = f"binding-{self._next_binding_number:08d}"
            self._next_binding_number += 1
            completion = CaptureCompletion()
            binding = object.__new__(CaptureBinding)
            object.__setattr__(binding, "binding_id", binding_id)
            object.__setattr__(binding, "run_id", normalized_run_id)
            object.__setattr__(
                binding,
                "hardware_id",
                normalized_hardware_id,
            )
            object.__setattr__(binding, "_owner_nonce", self._owner_nonce)
            object.__setattr__(binding, "_completion", completion)
            publisher = _CaptureCompletionPublisher(
                binding,
                completion,
            )
            self._bindings[binding_id] = _BindingRecord(
                binding=binding,
                publisher=publisher,
                generation_id=current_generation_id,
            )
            self._run_ids[normalized_run_id] = binding_id
            self._capture_permits += 1
            return binding

    def capture_completion(
        self,
        binding: CaptureBinding,
    ) -> CaptureCompletion:
        token = _validate_capture_binding(binding)
        with self._lock:
            if token._owner_nonce is not self._owner_nonce:
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "binding belongs to another resident pool",
                )
            record = self._bindings.get(token.binding_id)
            if record is not None and record.binding is token:
                return token._completion
            stored = self._authenticated_terminal_outcome(token)
            if stored is not None:
                return token._completion
            raise _request_error(
                ResidentErrorCode.INVALID,
                "binding is unknown or stale",
            )

    def start_capture(
        self,
        binding: CaptureBinding,
        *,
        trigger_monotonic_ns: int,
        target_frame_count: int,
    ) -> CaptureCompletion:
        token = _validate_capture_binding(binding)
        if type(trigger_monotonic_ns) is not int or trigger_monotonic_ns < 1:
            raise _request_error(
                ResidentErrorCode.INVALID,
                "trigger_monotonic_ns must be a positive integer",
            )
        if (
            type(target_frame_count) is not int
            or target_frame_count < 1
            or target_frame_count > self._max_capture_frames
        ):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "target_frame_count must be a positive bounded integer",
            )
        caller_now_ns = self._monotonic_ns()
        with self._lock:
            if token._owner_nonce is not self._owner_nonce:
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "binding belongs to another resident pool",
                )
            record = self._bindings.get(token.binding_id)
            if record is None or record.binding is not token:
                stored = self._authenticated_terminal_outcome(token)
                if stored is not None:
                    return token._completion
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "binding is unknown or stale",
                )
            if record.state == "ACTIVE":
                if (
                    record.trigger_monotonic_ns == trigger_monotonic_ns
                    and record.target_frame_count == target_frame_count
                ):
                    return token._completion
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "active capture parameters are already frozen",
                )
            if record.state != "RESERVED":
                raise RuntimeError("non-terminal binding state is invalid")
            slot = self._slots[token.hardware_id]
            if (
                not slot.ready
                or self._generation_ids.get(token.hardware_id)
                != record.generation_id
            ):
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    _fault_error(
                        phase="start capture",
                        hardware_id=token.hardware_id,
                        detail="stream is not ready",
                    ),
                )
                return token._completion
            spec = self._specs[token.hardware_id]
            future_allowance_ns = round(
                spec.frames_per_buffer * 1_000_000_000
                / spec.sample_rate_hz
            )
            if trigger_monotonic_ns > caller_now_ns + future_allowance_ns:
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "trigger exceeds the one-block monotonic clock allowance",
                )
            try:
                record.buffer = np.empty(
                    (
                        target_frame_count,
                        len(spec.capture_channel_indices),
                    ),
                    dtype=np.float32,
                )
            except Exception as exc:
                record.buffer = None
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.FAULT,
                        (
                            f"capture allocation {type(exc).__name__}: "
                            f"{_safe_detail(exc)}"
                        )[:MAX_BOUNDED_TEXT_CHARS],
                    ),
                )
                return token._completion
            try:
                mapped = slot.map_trigger(trigger_monotonic_ns)
            except Exception as exc:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.FAULT,
                        (
                            f"capture timeline map {type(exc).__name__}: "
                            f"{_safe_detail(exc)}"
                        )[:MAX_BOUNDED_TEXT_CHARS],
                    ),
                )
                return token._completion
            record.trigger_monotonic_ns = trigger_monotonic_ns
            record.target_frame_count = target_frame_count
            record.state = "ACTIVE"
            if mapped.status is _MappingStatus.AVAILABLE:
                record.start_cursor = mapped.cursor
                record.next_cursor = mapped.cursor
            elif mapped.status is _MappingStatus.EXPIRED:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.EXPIRED,
                        f"capture {token.binding_id} trigger expired",
                    ),
                )
            elif mapped.status is _MappingStatus.TIMELINE_GAP:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.FAULT,
                        f"capture {token.binding_id} TIMELINE_GAP",
                    ),
                )
            elif mapped.status is _MappingStatus.STREAM_FAULT:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.FAULT,
                        f"capture {token.binding_id} stream timeline fault",
                    ),
                )
            should_signal = token.binding_id in self._bindings
        if should_signal:
            slot.signal_work()
        return token._completion

    def _drain_stream(self, hardware_id: str) -> bool:
        more_work = False
        quarantined_slot: _ResidentStreamSlot | None = None
        fault_close_work: tuple[object, bool, int] | None = None
        recover_after_unlock = False
        with self._lock:
            slot = self._slots.get(hardware_id)
            if slot is None:
                return False
            try:
                fault, fault_monotonic_ns = (
                    slot.fault_details_under_decision_lock()
                )
                if fault is not None:
                    for binding_id in tuple(self._bindings):
                        record = self._bindings.get(binding_id)
                        if (
                            record is not None
                            and record.binding.hardware_id == hardware_id
                        ):
                            self._commit_terminal_locked(
                                record,
                                CaptureOutcomeStatus.FAILED,
                                fault,
                            )
                    quarantined_slot = slot
                else:
                    for binding_id in tuple(self._bindings):
                        record = self._bindings.get(binding_id)
                        if (
                            record is None
                            or record.state != "ACTIVE"
                            or record.binding.hardware_id != hardware_id
                        ):
                            continue
                        if self._progress_binding_locked(slot, record):
                            more_work = True
            except Exception as exc:
                fault = _fault_error(
                    phase="worker drain",
                    hardware_id=hardware_id,
                    detail=f"{type(exc).__name__}: {_safe_detail(exc)}",
                )
                slot.latch_owner_fault(fault)
                _, fault_monotonic_ns = (
                    slot.fault_details_under_decision_lock()
                )
                for binding_id in tuple(self._bindings):
                    record = self._bindings.get(binding_id)
                    if (
                        record is not None
                        and record.binding.hardware_id == hardware_id
                    ):
                        self._commit_terminal_locked(
                            record,
                            CaptureOutcomeStatus.FAILED,
                            fault,
                        )
                quarantined_slot = slot
            if quarantined_slot is not None:
                if fault_monotonic_ns is None:
                    raise RuntimeError("latched stream fault has no timestamp")
                recover_after_unlock = (
                    self._endpoint_resolver is not None
                    and hardware_id in self._endpoint_identities
                )
                if not recover_after_unlock:
                    handle, claimed = (
                        quarantined_slot.claim_close_attempt_under_decision_lock()
                    )
                    fault_close_work = (
                        handle,
                        claimed,
                        fault_monotonic_ns,
                    )
        if quarantined_slot is not None:
            if recover_after_unlock:
                self._request_runtime_recovery(hardware_id)
            else:
                assert fault_close_work is not None
                self._close_faulted_slot(
                    quarantined_slot,
                    handle=fault_close_work[0],
                    claimed=fault_close_work[1],
                    fault_monotonic_ns=fault_close_work[2],
                )
        return more_work

    def _close_faulted_slot(
        self,
        slot: _ResidentStreamSlot,
        *,
        handle: object,
        claimed: bool,
        fault_monotonic_ns: int,
    ) -> None:
        if not claimed:
            return
        hardware_id = slot._spec.hardware_id
        try:
            handle.request_stop()
        except Exception as exc:
            self._record_resource_error("fault request_stop", hardware_id, exc)
        deadline_ns = fault_monotonic_ns + int(
            self._fault_close_timeout_s * 1_000_000_000
        )
        try:
            result = handle.close(deadline_ns)
        except Exception as exc:
            self._record_resource_error("fault close", hardware_id, exc)
            result = BackendCloseResult(
                BackendCloseStatus.NOT_CONFIRMED,
                ResidentError(
                    ResidentErrorCode.NON_CLEAN,
                    (
                        f"fault close {hardware_id} {type(exc).__name__}: "
                        f"{_safe_detail(exc)}"
                    )[:MAX_BOUNDED_TEXT_CHARS],
                ),
            )
        if type(result) is not BackendCloseResult:
            invalid_result = TypeError("backend close returned an invalid result")
            self._record_resource_error(
                "fault close result",
                hardware_id,
                invalid_result,
            )
            result = BackendCloseResult(
                BackendCloseStatus.NOT_CONFIRMED,
                ResidentError(
                    ResidentErrorCode.NON_CLEAN,
                    f"fault close result {hardware_id} TypeError",
                ),
            )
        slot.record_close_result_from_control_owner(result)

    def _record_resource_error(
        self,
        phase: str,
        correlation: str,
        exc: Exception,
    ) -> None:
        error = ResidentError(
            ResidentErrorCode.NON_CLEAN,
            (
                f"{phase} {correlation} {type(exc).__name__}: "
                f"{_safe_detail(exc)}"
            )[:MAX_BOUNDED_TEXT_CHARS],
        )
        with self._lock:
            self._resource_errors.append(error)

    def _progress_binding_locked(
        self,
        slot: _ResidentStreamSlot,
        record: _BindingRecord,
    ) -> bool:
        if record.start_cursor is None:
            mapped = slot.map_trigger(record.trigger_monotonic_ns)
            if mapped.status is _MappingStatus.NOT_YET_AVAILABLE:
                return False
            if mapped.status is _MappingStatus.EXPIRED:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.EXPIRED,
                        f"capture {record.binding.binding_id} trigger expired",
                    ),
                )
                return False
            if mapped.status is _MappingStatus.TIMELINE_GAP:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.FAULT,
                        f"capture {record.binding.binding_id} TIMELINE_GAP",
                    ),
                )
                return False
            if mapped.status is _MappingStatus.STREAM_FAULT:
                self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.FAILED,
                    ResidentError(
                        ResidentErrorCode.FAULT,
                        f"capture {record.binding.binding_id} stream timeline fault",
                    ),
                )
                return False
            record.start_cursor = mapped.cursor
            record.next_cursor = mapped.cursor

        oldest_cursor, write_cursor = slot.ring_bounds()
        if record.next_cursor is None or record.buffer is None:
            return False
        if record.next_cursor < oldest_cursor:
            self._commit_terminal_locked(
                record,
                CaptureOutcomeStatus.FAILED,
                ResidentError(
                    ResidentErrorCode.EXPIRED,
                    f"capture {record.binding.binding_id} cursor expired",
                ),
            )
            return False
        target_frame_count = record.target_frame_count
        if target_frame_count is None:
            raise RuntimeError("ACTIVE binding target is missing")
        remaining = target_frame_count - record.copied_frame_count
        available = write_cursor - record.next_cursor
        copy_count = min(
            remaining,
            available,
            self._specs[record.binding.hardware_id].frames_per_buffer,
        )
        if copy_count <= 0:
            return False
        slot.copy_into(
            start_cursor=record.next_cursor,
            destination=record.buffer,
            destination_start=record.copied_frame_count,
            frame_count=copy_count,
        )
        record.next_cursor += copy_count
        record.copied_frame_count += copy_count
        if record.copied_frame_count == target_frame_count:
            self._commit_terminal_locked(
                record,
                CaptureOutcomeStatus.COMPLETED,
                None,
            )
            return False
        return record.next_cursor < write_cursor

    def cancel_capture(self, binding: CaptureBinding) -> CancelResult:
        token = _validate_capture_binding(binding)
        with self._lock:
            if token._owner_nonce is not self._owner_nonce:
                return self._invalid_cancel(
                    "binding belongs to another resident pool"
                )
            record = self._bindings.get(token.binding_id)
            if record is not None and record.binding is token:
                outcome, won = self._commit_terminal_locked(
                    record,
                    CaptureOutcomeStatus.CANCELLED,
                    ResidentError(
                        ResidentErrorCode.CANCELLED,
                        f"capture {token.binding_id} cancelled",
                    ),
                )
                if (
                    not won
                    or outcome.status is not CaptureOutcomeStatus.CANCELLED
                ):
                    return CancelResult(
                        CancelStatus.ALREADY_TERMINAL,
                        outcome=outcome,
                    )
                return CancelResult(CancelStatus.CANCELLED, outcome=outcome)
            stored = self._authenticated_terminal_outcome(token)
            if stored is not None:
                return CancelResult(
                    CancelStatus.ALREADY_TERMINAL,
                    outcome=stored,
                )
            return self._invalid_cancel("binding is unknown or stale")

    def _authenticated_terminal_outcome(
        self,
        binding: CaptureBinding,
    ) -> CaptureOutcome | None:
        completion = binding._completion
        proof = completion._terminal_proof
        if (
            type(proof) is not _BindingTerminalProof
            or proof.completion is not completion
            or proof.binding is not binding
        ):
            return None
        outcome = completion._stored_outcome()
        if (
            outcome is None
            or proof.outcome is not outcome
            or outcome.binding is not binding
        ):
            return None
        return outcome

    def _commit_terminal_locked(
        self,
        record: _BindingRecord,
        status: CaptureOutcomeStatus,
        error: ResidentError | None,
    ) -> tuple[CaptureOutcome, bool]:
        """Return ``(winning_outcome, caller_won)`` under the pool lock."""
        binding = record.binding
        active = self._bindings.get(binding.binding_id)
        if active is not record:
            stored = self._authenticated_terminal_outcome(binding)
            if stored is None:
                raise RuntimeError("terminal candidate has no winner")
            return stored, False
        audio: CapturedAudio | None = None
        if status is CaptureOutcomeStatus.COMPLETED:
            if (
                record.state != "ACTIVE"
                or record.buffer is None
                or record.target_frame_count is None
                or record.copied_frame_count != record.target_frame_count
                or record.start_cursor is None
                or record.trigger_monotonic_ns is None
                or error is not None
            ):
                raise RuntimeError("completed capture is not exact")
            try:
                record.buffer.setflags(write=False)
                spec = self._specs[binding.hardware_id]
                audio = CapturedAudio(
                    binding=binding,
                    run_id=binding.run_id,
                    hardware_id=binding.hardware_id,
                    trigger_monotonic_ns=record.trigger_monotonic_ns,
                    start_frame_cursor=record.start_cursor,
                    frame_count=record.target_frame_count,
                    sample_rate_hz=spec.sample_rate_hz,
                    channel_indices=spec.capture_channel_indices,
                    samples=record.buffer,
                    completion_monotonic_ns=self._monotonic_ns(),
                )
            except Exception as exc:
                audio = None
                status = CaptureOutcomeStatus.FAILED
                error = ResidentError(
                    ResidentErrorCode.FAULT,
                    (
                        f"outcome preparation {type(exc).__name__}: "
                        f"{_safe_detail(exc)}"
                    )[:MAX_BOUNDED_TEXT_CHARS],
                )
            record.buffer = None
        else:
            record.buffer = None
            if error is None:
                raise RuntimeError("non-completed terminal capture needs an error")
        try:
            outcome = CaptureOutcome(
                status=status,
                binding=binding,
                audio=audio,
                error=error,
            )
        except Exception as exc:
            audio = None
            status = CaptureOutcomeStatus.FAILED
            error = ResidentError(
                ResidentErrorCode.FAULT,
                (
                    f"outcome construction {type(exc).__name__}: "
                    f"{_safe_detail(exc)}"
                )[:MAX_BOUNDED_TEXT_CHARS],
            )
            outcome = CaptureOutcome(
                status=status,
                binding=binding,
                audio=None,
                error=error,
            )
        record.publisher.prepare(outcome)
        record.publisher.publish()
        del self._bindings[binding.binding_id]
        del self._run_ids[binding.run_id]
        self._capture_permits -= 1
        return outcome, True

    @staticmethod
    def _invalid_cancel(summary: str) -> CancelResult:
        error = ResidentError(
            ResidentErrorCode.INVALID,
            summary[:MAX_BOUNDED_TEXT_CHARS],
        )
        return CancelResult(CancelStatus.INVALID, error=error)

    def maintenance_readiness(
        self,
        target_hardware_ids: Collection[str] | None = None,
    ) -> MaintenanceReadiness:
        if target_hardware_ids is None:
            targets = tuple(sorted(self._specs))
        else:
            if isinstance(
                target_hardware_ids, (str, bytes, bytearray)
            ) or not isinstance(
                target_hardware_ids, Collection
            ):
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "hardware_ids must be None or a collection of ids",
                )
            normalized: list[str] = []
            for hardware_id in target_hardware_ids:
                normalized.append(
                    _validate_request_text(hardware_id, "hardware_id")
                )
            if len(normalized) != len(set(normalized)):
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    "hardware_ids must not contain duplicates",
                )
            unknown = sorted(set(normalized) - set(self._specs))
            if unknown:
                raise _request_error(
                    ResidentErrorCode.INVALID,
                    f"unknown hardware_id {unknown[0]}",
                )
            targets = tuple(sorted(normalized))

        with self._lock:
            if self._state == "CLOSED":
                report = self._shutdown_report
                unclosed = (
                    ()
                    if report is None
                    else tuple(
                        hardware_id
                        for hardware_id in report.unclosed_hardware_ids
                        if hardware_id in targets
                    )
                )
                reason = (
                    "resident pool is closed; restart is required"
                    if report is not None
                    and report.status is ShutdownStatus.NON_CLEAN
                    else "resident pool is closed"
                )
                return MaintenanceReadiness(
                    MaintenanceStatus.CLOSED,
                    (),
                    unclosed,
                    reason,
                )
            if self._state == "CLOSING":
                return MaintenanceReadiness(
                    MaintenanceStatus.CLOSING,
                    tuple(sorted(self._bindings)),
                    (),
                    "resident pool shutdown is in progress",
                )
            active = tuple(sorted(self._bindings))
            if active:
                return MaintenanceReadiness(
                    MaintenanceStatus.BUSY,
                    active,
                    (),
                    "resident captures are active",
                )
            owned = tuple(
                hardware_id
                for hardware_id in targets
                if (
                    (slot := self._slots.get(hardware_id)) is not None
                    and self._slot_owns_hardware(slot)
                    or any(
                        orphan.hardware_id == hardware_id
                        for orphan in self._orphans
                    )
                )
            )
            if owned:
                return MaintenanceReadiness(
                    MaintenanceStatus.BUSY,
                    (),
                    owned,
                    "requested hardware is owned by the resident pool",
                )
            return MaintenanceReadiness(
                MaintenanceStatus.READY,
                (),
                (),
                "requested hardware has no resident blocker",
            )

    @staticmethod
    def _slot_owns_hardware(slot: _ResidentStreamSlot) -> bool:
        if slot._handle is None:
            return False
        close_result = slot.close_result_snapshot()
        return (
            close_result is None
            or close_result.status is BackendCloseStatus.NOT_CONFIRMED
        )

    def shutdown(
        self,
        *,
        deadline_monotonic_ns: int | None = None,
    ) -> ShutdownReport:
        now_ns = self._monotonic_ns()
        if deadline_monotonic_ns is None:
            deadline_ns = now_ns + int(
                self._shutdown_timeout_s * 1_000_000_000
            )
        elif (
            type(deadline_monotonic_ns) is not int
            or deadline_monotonic_ns <= now_ns
            or deadline_monotonic_ns > (1 << 63) - 1
        ):
            raise _request_error(
                ResidentErrorCode.INVALID,
                "deadline_monotonic_ns must be None or a future positive integer",
            )
        else:
            deadline_ns = deadline_monotonic_ns

        with self._shutdown_gate:
            if self._shutdown_report is not None:
                return self._shutdown_report
            if self._shutdown_owner:
                assert self._shutdown_started_ns is not None
                assert self._shutdown_deadline_ns is not None
                return ShutdownReport(
                    ShutdownStatus.IN_PROGRESS,
                    self._shutdown_started_ns,
                    None,
                    tuple(sorted(self._slots)),
                    tuple(
                        sorted(
                            owner.worker_id
                            for slot in self._slots.values()
                            if (owner := slot._drain_owner) is not None
                        )
                    ),
                    tuple(sorted(self._bindings)),
                    (),
                    self._shutdown_deadline_ns,
                    "等待 owning shutdown caller 的最终报告",
                )
            self._shutdown_owner = True
            self._shutdown_started_ns = now_ns
            self._shutdown_deadline_ns = deadline_ns

        close_work: list[
            tuple[str, _ResidentStreamSlot, object, bool]
        ] = []
        recovery_workers: list[tuple[str, threading.Thread]] = []
        retired_drains: list[tuple[str, _ResidentStreamSlot]] = []
        with self._lock:
            self._state = "CLOSING"
            for hardware_id, recovery in self._recovery_states.items():
                recovery.stop.set()
                recovery.wake.set()
                self._settle_recovery_locked(
                    hardware_id,
                    LifecycleOutcome(
                        LifecycleStatus.LIFECYCLE_CANCELLED,
                        diagnostic=self._diagnostic(
                            DiagnosticStage.LIFECYCLE,
                            DiagnosticReason.CLOSING,
                            "resident pool shutdown cancelled recovery",
                        ),
                    ),
                )
                if recovery.worker is not None:
                    recovery_workers.append((hardware_id, recovery.worker))
            for slot in self._slots.values():
                slot.invalidate_generation_under_decision_lock("CLOSING")
            for binding_id in tuple(self._bindings):
                record = self._bindings.get(binding_id)
                if record is not None:
                    self._commit_terminal_locked(
                        record,
                        CaptureOutcomeStatus.CANCELLED,
                        ResidentError(
                            ResidentErrorCode.CANCELLED,
                            f"capture {binding_id} cancelled by shutdown",
                        ),
                    )
            for hardware_id in sorted(self._slots):
                slot = self._slots[hardware_id]
                handle, claimed = (
                    slot.claim_close_attempt_under_decision_lock()
                )
                close_work.append((hardware_id, slot, handle, claimed))

        unclosed: list[str] = []
        unjoined: list[str] = []
        for hardware_id, slot, handle, claimed in close_work:
            if claimed:
                try:
                    handle.request_stop()
                except Exception as exc:
                    self._record_resource_error(
                        "shutdown request_stop", hardware_id, exc
                    )
                try:
                    close_result = handle.close(deadline_ns)
                except Exception as exc:
                    self._record_resource_error("shutdown close", hardware_id, exc)
                    close_result = BackendCloseResult(
                        BackendCloseStatus.NOT_CONFIRMED,
                        ResidentError(
                            ResidentErrorCode.NON_CLEAN,
                            (
                                f"shutdown close {hardware_id} "
                                f"{type(exc).__name__}: {_safe_detail(exc)}"
                            )[:MAX_BOUNDED_TEXT_CHARS],
                        ),
                    )
                if type(close_result) is not BackendCloseResult:
                    invalid_result = TypeError(
                        "backend close returned an invalid result"
                    )
                    self._record_resource_error(
                        "shutdown close result",
                        hardware_id,
                        invalid_result,
                    )
                    close_result = BackendCloseResult(
                        BackendCloseStatus.NOT_CONFIRMED,
                        ResidentError(
                            ResidentErrorCode.NON_CLEAN,
                            f"shutdown close result {hardware_id} TypeError",
                        ),
                    )
                slot.record_close_result_from_control_owner(close_result)
            close_result = slot.close_result_snapshot()
            if handle is not None and (
                close_result is None
                or close_result.status is not BackendCloseStatus.CLOSED
            ):
                unclosed.append(hardware_id)

        for hardware_id in sorted(self._slots):
            slot = self._slots[hardware_id]
            slot.stop_worker()

        for hardware_id in sorted(self._slots):
            slot = self._slots[hardware_id]
            worker_id = slot.worker_id()
            if worker_id is None:
                continue
            remaining_s = max(
                0.0,
                (deadline_ns - self._monotonic_ns()) / 1_000_000_000,
            )
            try:
                joined = slot.join_worker(remaining_s)
            except Exception as exc:
                self._record_resource_error("shutdown join", worker_id, exc)
                joined = False
            if not joined:
                unjoined.append(worker_id)

        for hardware_id, worker in recovery_workers:
            remaining_s = max(
                0.0,
                (deadline_ns - self._monotonic_ns()) / 1_000_000_000,
            )
            worker.join(remaining_s)
            if worker.is_alive():
                unjoined.append(f"recovery:{hardware_id}")

        with self._lock:
            retired_drains = list(self._retired_drains.items())
        for worker_id, retired_slot in retired_drains:
            remaining_s = max(
                0.0,
                (deadline_ns - self._monotonic_ns()) / 1_000_000_000,
            )
            if retired_slot.join_worker(remaining_s):
                with self._lock:
                    self._retired_drains.pop(worker_id, None)
            else:
                unjoined.append(worker_id)

        with self._lock:
            orphan_records = tuple(self._orphans)
        for orphan in orphan_records:
            lifecycle_value = getattr(
                getattr(orphan.handle, "lifecycle_state", None),
                "value",
                None,
            )
            if lifecycle_value == "CALL_TIMED_OUT":
                remaining_s = max(
                    0.0,
                    (deadline_ns - self._monotonic_ns()) / 1_000_000_000,
                )
                waiter = getattr(
                    orphan.handle,
                    "wait_for_lifecycle_settle",
                    None,
                )
                if callable(waiter) and remaining_s > 0:
                    try:
                        waiter(remaining_s)
                    except Exception as exc:
                        self._record_resource_error(
                            "shutdown orphan wait",
                            orphan.hardware_id,
                            exc,
                        )
                lifecycle_value = getattr(
                    getattr(orphan.handle, "lifecycle_state", None),
                    "value",
                    None,
                )
            if lifecycle_value == "CLOSED":
                with self._lock:
                    if orphan in self._orphans:
                        self._orphans.remove(orphan)
                continue
            remaining_ns = deadline_ns - self._monotonic_ns()
            if lifecycle_value != "CALL_TIMED_OUT" and remaining_ns > 0:
                try:
                    orphan.handle.request_stop()
                    orphan_result = orphan.handle.close(deadline_ns)
                except Exception as exc:
                    self._record_resource_error(
                        "shutdown orphan close", orphan.hardware_id, exc
                    )
                    orphan_result = None
                if (
                    type(orphan_result) is BackendCloseResult
                    and orphan_result.status is BackendCloseStatus.CLOSED
                ):
                    with self._lock:
                        if orphan in self._orphans:
                            self._orphans.remove(orphan)

        resolver_shutdown = getattr(
            self._endpoint_resolver,
            "shutdown",
            None,
        )
        if callable(resolver_shutdown):
            try:
                scan_workers = resolver_shutdown(deadline_ns)
                if type(scan_workers) is not tuple or not all(
                    type(worker_id) is str and worker_id
                    for worker_id in scan_workers
                ):
                    raise TypeError(
                        "endpoint resolver shutdown returned invalid worker ids"
                    )
                unjoined.extend(scan_workers)
            except Exception as exc:
                self._record_resource_error(
                    "shutdown endpoint resolver",
                    "endpoint-scans",
                    exc,
                )

        unclosed = [
            hardware_id
            for hardware_id, slot, handle, _claimed in close_work
            if handle is not None
            and (
                (close_result := slot.close_result_snapshot()) is None
                or close_result.status is not BackendCloseStatus.CLOSED
            )
        ]
        with self._lock:
            unclosed.extend(orphan.hardware_id for orphan in self._orphans)
        finished_ns = max(self._monotonic_ns(), now_ns)
        with self._lock:
            non_terminal = tuple(sorted(self._bindings))
            errors = tuple(self._resource_errors) + tuple(
                orphan.last_error
                for orphan in self._orphans
                if orphan.last_error is not None
            )
        unclosed_ids = tuple(sorted(set(unclosed)))
        unjoined_ids = tuple(sorted(set(unjoined)))
        clean = not (unclosed_ids or unjoined_ids or non_terminal or errors)
        report = ShutdownReport(
            ShutdownStatus.CLOSED if clean else ShutdownStatus.NON_CLEAN,
            now_ns,
            finished_ns,
            unclosed_ids,
            unjoined_ids,
            non_terminal,
            errors,
            deadline_ns,
            "" if clean else "不要做相关 maintenance；停止并重启进程",
        )
        with self._lock:
            self._shutdown_report = report
            self._state = "CLOSED"
        return report
