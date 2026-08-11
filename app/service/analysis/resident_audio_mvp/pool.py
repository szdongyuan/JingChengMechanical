"""Pool startup and bounded capture admission."""

from __future__ import annotations

import dataclasses
import math
import threading
import time
from collections.abc import Collection, Sequence
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
    MaintenanceReadiness,
    MaintenanceStatus,
    OpenResult,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
    ShutdownReport,
    ShutdownStatus,
    StreamOpenResult,
    StreamOpenStatus,
    _validate_capture_binding,
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
        pool = cls(
            specs=tuple(spec for spec, _ in layouts),
            backend=backend,
            max_capture_frames=max_capture_frames,
            startup_stream_timeout_s=startup_timeout,
            fault_close_timeout_s=fault_timeout,
            shutdown_timeout_s=shutdown_timeout,
        )
        reports: list[StreamOpenResult] = []
        for spec, ring_frames in layouts:
            report = pool._open_slot(spec, ring_frames)
            reports.append(report)
        pool._state = "OPEN"
        return OpenResult(
            pool=pool,
            streams=tuple(reports),
            ready_count=sum(
                report.status is StreamOpenStatus.READY for report in reports
            ),
        )

    def _open_slot(
        self,
        spec: ResidentStreamSpec,
        ring_frames: int,
    ) -> StreamOpenResult:
        try:
            slot = _ResidentStreamSlot(
                spec,
                ring_capacity_frames=ring_frames,
                monotonic_ns=time.monotonic_ns,
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
        slot.publish_drain_owner(self._drain_stream)
        deadline_ns = time.monotonic_ns() + int(
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
            remaining_ns = max(0, deadline_ns - time.monotonic_ns())
            if remaining_ns:
                slot._first_block_event.wait(remaining_ns / 1_000_000_000)
        owner_monotonic_ns = time.monotonic_ns()
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
            else time.monotonic_ns()
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

    def reserve_capture(self, run_id: str, hardware_id: str) -> CaptureBinding:
        normalized_run_id = _validate_request_text(run_id, "run_id")
        normalized_hardware_id = _validate_request_text(
            hardware_id,
            "hardware_id",
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
        caller_now_ns = time.monotonic_ns()
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
            if not slot.ready:
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
                handle, claimed = (
                    quarantined_slot.claim_close_attempt_under_decision_lock()
                )
                fault_close_work = (
                    handle,
                    claimed,
                    fault_monotonic_ns,
                )
        if quarantined_slot is not None:
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
                    completion_monotonic_ns=time.monotonic_ns(),
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
        now_ns = time.monotonic_ns()
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
        with self._lock:
            self._state = "CLOSING"
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
                (deadline_ns - time.monotonic_ns()) / 1_000_000_000,
            )
            try:
                joined = slot.join_worker(remaining_s)
            except Exception as exc:
                self._record_resource_error("shutdown join", worker_id, exc)
                joined = False
            if not joined:
                unjoined.append(worker_id)

        unclosed = [
            hardware_id
            for hardware_id, slot, handle, _claimed in close_work
            if handle is not None
            and (
                (close_result := slot.close_result_snapshot()) is None
                or close_result.status is not BackendCloseStatus.CLOSED
            )
        ]
        finished_ns = max(time.monotonic_ns(), now_ns)
        with self._lock:
            non_terminal = tuple(sorted(self._bindings))
            errors = tuple(self._resource_errors)
        clean = not (unclosed or unjoined or non_terminal or errors)
        report = ShutdownReport(
            ShutdownStatus.CLOSED if clean else ShutdownStatus.NON_CLEAN,
            now_ns,
            finished_ns,
            tuple(sorted(unclosed)),
            tuple(sorted(unjoined)),
            non_terminal,
            errors,
            deadline_ns,
            "" if clean else "不要做相关 maintenance；停止并重启进程",
        )
        with self._lock:
            self._shutdown_report = report
            self._state = "CLOSED"
        return report
