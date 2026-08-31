from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import final

from app.service.analysis.resident_audio_mvp import (
    CaptureBinding,
    CancelResult,
    CancelStatus,
    CapturedAudio,
    CaptureOutcome as ResidentCaptureOutcome,
    CaptureOutcomeStatus as ResidentCaptureOutcomeStatus,
    LifecycleOutcome,
    LifecycleStatus,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
)
from app.service.managed_input_contracts import (
    ManagedHardwareId,
    ResidentStreamGenerationStamp,
)
from app.service.managed_input_resident_adapter import (
    ResidentCaptureTarget,
    ResidentManagedInputAdapter,
)

from .contracts import (
    CapturedAudioValue,
    CapturedSourceFormat,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CaptureReservation,
    CaptureReserveClaim,
    CaptureReservePending,
    CaptureReserveResult,
    CaptureReserveStatus,
    CaptureReserveWakeSignal,
    ResidentCancelResult,
    ResidentCancelStatus,
    TerminalErrorCategory,
    _issue_capture_reserve_claim,
    _issue_capture_reserve_wake_signal,
    _validate_capture_reserve_claim,
)


_LOGGER = logging.getLogger(__name__)


_MAX_RESERVATIONS = 6
_MAX_CLOSE_SECONDS = 60.0


def _bounded_reason(value: object, fallback: str) -> str:
    text = str(value) if value is not None else fallback
    if not text:
        text = fallback
    encoded = text.encode("utf-8")[:512]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]


@final
@dataclass(frozen=True, slots=True)
class ManagedResidentCaptureRoute:
    coordinator_hardware_id: str
    managed_hardware_id: ManagedHardwareId
    generation: ResidentStreamGenerationStamp

    def __post_init__(self) -> None:
        if (
            type(self.coordinator_hardware_id) is not str
            or not self.coordinator_hardware_id
        ):
            raise ValueError("coordinator_hardware_id must be nonempty text")
        if type(self.managed_hardware_id) is not ManagedHardwareId:
            raise ValueError("managed_hardware_id must be an exact ManagedHardwareId")
        if type(self.generation) is not ResidentStreamGenerationStamp:
            raise ValueError(
                "generation must be an exact ResidentStreamGenerationStamp"
            )
        if self.generation.hardware_id != self.managed_hardware_id:
            raise ValueError("generation belongs to another managed hardware ID")


class ManagedResidentCaptureCloseStatus(Enum):
    CLOSED = auto()
    NON_CLEAN = auto()


@final
@dataclass(frozen=True, slots=True)
class ManagedResidentCaptureCloseReport:
    status: ManagedResidentCaptureCloseStatus
    remaining_registration_ids: tuple[str, ...]
    remaining_reservation_ids: tuple[str, ...]
    remaining_waiter_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not ManagedResidentCaptureCloseStatus:
            raise ValueError("status must be an exact close status")
        for field_name in (
            "remaining_registration_ids",
            "remaining_reservation_ids",
            "remaining_waiter_ids",
        ):
            values = getattr(self, field_name)
            if (
                type(values) is not tuple
                or any(type(value) is not str or not value for value in values)
                or values != tuple(sorted(set(values)))
                or len(values) > _MAX_RESERVATIONS
            ):
                raise ValueError(f"{field_name} must be a sorted bounded ID tuple")
        reservations = set(self.remaining_reservation_ids)
        if (
            not set(self.remaining_registration_ids).issubset(reservations)
            or not set(self.remaining_waiter_ids).issubset(reservations)
        ):
            raise ValueError("registration and waiter IDs must remain reserved")
        if self.status is ManagedResidentCaptureCloseStatus.CLOSED:
            if (
                self.remaining_registration_ids
                or self.remaining_reservation_ids
                or self.remaining_waiter_ids
            ):
                raise ValueError("CLOSED cannot carry remaining identities")
        elif not self.remaining_reservation_ids:
            raise ValueError("NON_CLEAN requires a remaining reservation")


class _Lifecycle(Enum):
    OPEN = auto()
    CLOSING = auto()
    CLOSED = auto()


class _ReservationState(Enum):
    RESERVED_UNREGISTERED = auto()
    REGISTERING = auto()
    REGISTERED_WAITING = auto()
    STARTED = auto()
    TERMINAL_DELIVERING = auto()
    RETIRED = auto()
    REGISTERING_CANCEL_REQUESTED = auto()
    REGISTRATION_FAILED_AWAITING_CANCEL = auto()
    CANCELING_UNREGISTERED = auto()
    CLOSE_CANCELING = auto()
    NON_CLEAN = auto()


@dataclass(slots=True)
class _ReservationEntry:
    reservation: CaptureReservation
    target: ResidentCaptureTarget
    binding: CaptureBinding
    state: _ReservationState = _ReservationState.RESERVED_UNREGISTERED
    completion: object | None = None
    sink: Callable[[CaptureOutcome], None] | None = None
    waiter: threading.Thread | None = None
    cancel_requested: bool = False
    cancel_settlement: str | None = None
    close_requested: bool = False
    start_claimed: bool = False
    start_in_flight: bool = False
    start_settled_at: float | None = None


@dataclass(slots=True)
class _ReserveClaim:
    run_id: str
    report_id: str
    token: CaptureReserveClaim | None = None
    route: ManagedResidentCaptureRoute | None = None
    target: ResidentCaptureTarget | None = None
    readiness_ticket: object | None = None
    wake_signal: CaptureReserveWakeSignal | None = None
    cancel_requested: bool = False
    in_flight: bool = True
    settled_at: float | None = None


@final
class ManagedResidentCapturePort:
    def __init__(
        self,
        managed_input: ResidentManagedInputAdapter,
        routes: tuple[ManagedResidentCaptureRoute, ...],
        *,
        readiness_probe: Callable[[str], bool] | None = None,
    ) -> None:
        if type(routes) is not tuple or not 1 <= len(routes) <= _MAX_RESERVATIONS:
            raise ValueError("routes must contain one through six entries")
        if any(type(route) is not ManagedResidentCaptureRoute for route in routes):
            raise ValueError("routes must contain exact route values")
        coordinator_ids = tuple(route.coordinator_hardware_id for route in routes)
        managed_ids = tuple(route.managed_hardware_id for route in routes)
        if (
            len(set(coordinator_ids)) != len(coordinator_ids)
            or len(set(managed_ids)) != len(managed_ids)
        ):
            raise ValueError("route identities must be unique")
        if readiness_probe is not None and not callable(readiness_probe):
            raise TypeError("readiness_probe must be callable or None")

        self._managed_input = managed_input
        self._readiness_probe = readiness_probe
        self._routes = {
            route.coordinator_hardware_id: route for route in routes
        }
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._lifecycle = _Lifecycle.OPEN
        self._claim_owner_nonce = object()
        self._next_claim_number = 1
        self._reserve_claims: dict[str, _ReserveClaim] = {}
        self._reservations: dict[str, _ReservationEntry] = {}
        self._clean_close_report: ManagedResidentCaptureCloseReport | None = None

    def begin_reserve(
        self,
        run_id: str,
        hardware_id: str,
        trigger_monotonic_ns: int,
        deadline_monotonic_ns: int,
    ) -> CaptureReserveResult | CaptureReservePending:
        with self._lock:
            if self._lifecycle is not _Lifecycle.OPEN:
                return self._reserve_failure(
                    CaptureReserveStatus.CLOSING,
                    run_id,
                    hardware_id,
                    "managed Resident capture port is closing",
                )
            route = self._routes.get(hardware_id)
            if route is None:
                return self._reserve_failure(
                    CaptureReserveStatus.NOT_READY,
                    run_id,
                    hardware_id,
                    "capture route is not configured",
                )
            if self._run_has_capacity_owner_locked(run_id):
                return self._reserve_failure(
                    CaptureReserveStatus.FAULT,
                    run_id,
                    hardware_id,
                    "admission/duplicate_run_id: run already owns capture capacity",
                )
            if (
                len(self._reservations) + len(self._reserve_claims)
                >= _MAX_RESERVATIONS
            ):
                return self._reserve_failure(
                    CaptureReserveStatus.CAPACITY,
                    run_id,
                    hardware_id,
                    "admission/capture_capacity: managed Resident capacity is six",
                )
            claim_number = self._next_claim_number
            self._next_claim_number += 1
            token = _issue_capture_reserve_claim(
                claim_id=f"resident-reserve-{claim_number:08d}",
                run_id=run_id,
                hardware_id=hardware_id,
                trigger_monotonic_ns=trigger_monotonic_ns,
                deadline_monotonic_ns=deadline_monotonic_ns,
                owner_nonce=self._claim_owner_nonce,
            )
            claim = _ReserveClaim(
                run_id=run_id,
                report_id=token.claim_id,
                token=token,
                route=route,
            )
            self._reserve_claims[run_id] = claim

        try:
            target = self._managed_input.capture_target(
                route.managed_hardware_id,
                route.generation,
            )
            self._validate_target(target, route)
            ticket = target.pool.begin_readiness(
                target.resident_hardware_id,
                deadline_monotonic_ns,
            )
            future = ticket.future
            outcome = future.result_if_ready()
        except Exception as exc:
            cancelled = self._release_claim(claim)
            if cancelled:
                return self._reserve_failure(
                    CaptureReserveStatus.CLOSING,
                    run_id,
                    hardware_id,
                    "lifecycle/closing: reserve claim was cancelled",
                )
            return self._reserve_exception(exc, run_id, hardware_id)

        claim.target = target
        claim.readiness_ticket = ticket
        if outcome is not None:
            return self._complete_reserve_claim(claim, outcome)

        wake_signal = _issue_capture_reserve_wake_signal(
            claim=token,
            owner_nonce=self._claim_owner_nonce,
            wait=future.wait,
        )
        with self._lock:
            current = self._reserve_claims.get(run_id)
            if (
                current is not claim
                or claim.cancel_requested
                or self._lifecycle is not _Lifecycle.OPEN
            ):
                self._release_claim_locked(claim)
                return self._reserve_failure(
                    CaptureReserveStatus.CLOSING,
                    run_id,
                    hardware_id,
                    "lifecycle/closing: capture port closed during readiness",
                )
            claim.wake_signal = wake_signal
            claim.in_flight = False
            claim.settled_at = time.monotonic()
            self._condition.notify_all()
        return CaptureReservePending(
            run_id,
            hardware_id,
            token,
            wake_signal,
        )

    def resume_reserve(
        self,
        claim: CaptureReserveClaim,
    ) -> CaptureReserveResult | CaptureReservePending:
        token = _validate_capture_reserve_claim(
            claim,
            owner_nonce=self._claim_owner_nonce,
        )
        with self._lock:
            current = self._reserve_claims.get(token.run_id)
            if current is None or current.token is not token:
                if self._lifecycle is not _Lifecycle.OPEN:
                    return self._reserve_failure(
                        CaptureReserveStatus.CLOSING,
                        token.run_id,
                        token.hardware_id,
                        "lifecycle/closing: reserve continuation was cancelled",
                    )
                raise ValueError("capture reserve claim is unknown or already settled")
            if current.in_flight:
                raise ValueError("capture reserve claim is already being resumed")
            if current.target is None or current.readiness_ticket is None:
                raise RuntimeError("capture reserve claim is not resumable")
            current.in_flight = True
            current.settled_at = None
            future = current.readiness_ticket.future

        try:
            remaining_ns = token.deadline_monotonic_ns - time.monotonic_ns()
            outcome = (
                None
                if remaining_ns <= 0
                else future.wait(remaining_ns / 1_000_000_000)
            )
        except Exception as exc:
            cancelled = self._release_claim(current)
            return self._reserve_failure(
                (
                    CaptureReserveStatus.CLOSING
                    if cancelled
                    else CaptureReserveStatus.FAULT
                ),
                token.run_id,
                token.hardware_id,
                (
                    "lifecycle/closing: reserve claim was cancelled"
                    if cancelled
                    else _bounded_reason(
                        exc,
                        "Resident readiness continuation failed",
                    )
                ),
            )
        if outcome is None:
            cancelled = self._release_claim(current)
            return self._reserve_failure(
                (
                    CaptureReserveStatus.CLOSING
                    if cancelled
                    else CaptureReserveStatus.NOT_READY
                ),
                token.run_id,
                token.hardware_id,
                (
                    "lifecycle/closing: reserve claim was cancelled"
                    if cancelled
                    else (
                        "background_retry/retry_scheduled: Resident readiness "
                        "deadline elapsed"
                    )
                ),
            )
        return self._complete_reserve_claim(current, outcome)

    def cancel_reserve(self, claim: CaptureReserveClaim) -> bool:
        token = _validate_capture_reserve_claim(
            claim,
            owner_nonce=self._claim_owner_nonce,
        )
        with self._lock:
            current = self._reserve_claims.get(token.run_id)
            if (
                current is None
                or current.token is not token
                or current.cancel_requested
            ):
                return False
            current.cancel_requested = True
            if current.wake_signal is not None:
                current.wake_signal._cancel()
            if not current.in_flight:
                self._release_claim_locked(current)
            else:
                self._condition.notify_all()
            return True

    def reserve(self, run_id: str, hardware_id: str) -> CaptureReserveResult:
        with self._lock:
            if self._lifecycle is not _Lifecycle.OPEN:
                return self._reserve_failure(
                    CaptureReserveStatus.CLOSING,
                    run_id,
                    hardware_id,
                    "managed Resident capture port is closing",
                )
            route = self._routes.get(hardware_id)
            if route is None:
                return self._reserve_failure(
                    CaptureReserveStatus.NOT_READY,
                    run_id,
                    hardware_id,
                    "capture route is not configured",
                )
            if self._readiness_probe is not None:
                try:
                    input_ready = self._readiness_probe(hardware_id) is True
                except Exception as exc:
                    return self._reserve_failure(
                        CaptureReserveStatus.NOT_READY,
                        run_id,
                        hardware_id,
                        _bounded_reason(
                            "liveness/backend_fault: readiness probe "
                            f"{type(exc).__name__}: {exc}",
                            "liveness/backend_fault: readiness probe failed",
                        ),
                    )
                if not input_ready:
                    return self._reserve_failure(
                        CaptureReserveStatus.NOT_READY,
                        run_id,
                        hardware_id,
                        "liveness/heartbeat_stale: "
                        "readiness probe reported unavailable",
                    )
            if run_id in self._reserve_claims:
                return self._reserve_failure(
                    CaptureReserveStatus.FAULT,
                    run_id,
                    hardware_id,
                    "run already has an in-flight capture reservation",
                )
            if (
                len(self._reservations) + len(self._reserve_claims)
                >= _MAX_RESERVATIONS
            ):
                return self._reserve_failure(
                    CaptureReserveStatus.CAPACITY,
                    run_id,
                    hardware_id,
                    "managed Resident capture capacity is six",
                )
            claim = _ReserveClaim(run_id=run_id, report_id=run_id)
            self._reserve_claims[run_id] = claim

        try:
            target = self._managed_input.capture_target(
                route.managed_hardware_id,
                route.generation,
            )
            self._validate_target(target, route)
            binding = target.pool.reserve_capture(
                run_id,
                target.resident_hardware_id,
            )
            self._validate_binding(binding, run_id, target.resident_hardware_id)
            reservation = CaptureReservation(
                reservation_id=binding.binding_id,
                run_id=run_id,
                hardware_id=hardware_id,
            )
        except Exception as exc:
            settled_at = time.monotonic()
            with self._lock:
                if self._reserve_claims.get(run_id) is claim:
                    claim.in_flight = False
                    claim.settled_at = settled_at
                    del self._reserve_claims[run_id]
                self._condition.notify_all()
            return self._reserve_exception(exc, run_id, hardware_id)

        entry = _ReservationEntry(
            reservation=reservation,
            target=target,
            binding=binding,
        )
        with self._lock:
            current_claim = self._reserve_claims.get(run_id)
            if current_claim is not claim:
                raise RuntimeError("reserve claim ownership was lost")
            if (
                self._lifecycle is _Lifecycle.OPEN
                and reservation.reservation_id not in self._reservations
            ):
                self._reservations[reservation.reservation_id] = entry
                del self._reserve_claims[run_id]
                self._condition.notify_all()
                return CaptureReserveResult(
                    status=CaptureReserveStatus.RESERVED,
                    run_id=run_id,
                    hardware_id=hardware_id,
                    reservation=reservation,
                    reason="",
                )
            current_claim.report_id = reservation.reservation_id
            late_status = (
                CaptureReserveStatus.CLOSING
                if self._lifecycle is not _Lifecycle.OPEN
                else CaptureReserveStatus.FAULT
            )
            self._condition.notify_all()

        _, clean = self._call_cancel(entry)
        settled_at = time.monotonic()
        with self._lock:
            current_claim = self._reserve_claims.get(run_id)
            if current_claim is claim:
                current_claim.in_flight = False
                current_claim.settled_at = settled_at
                if clean:
                    del self._reserve_claims[run_id]
                elif reservation.reservation_id not in self._reservations:
                    entry.state = _ReservationState.NON_CLEAN
                    entry.cancel_requested = True
                    entry.cancel_settlement = "NON_CLEAN"
                    self._reservations[reservation.reservation_id] = entry
                    del self._reserve_claims[run_id]
                else:
                    current_claim.report_id = run_id
                    current_claim.in_flight = False
                self._condition.notify_all()
        return self._reserve_failure(
            late_status,
            run_id,
            hardware_id,
            (
                "capture port closed while Resident reservation was in flight"
                if late_status is CaptureReserveStatus.CLOSING
                else "Resident returned a duplicate binding"
            ),
        )

    def register_completion(
        self,
        reservation: CaptureReservation,
        sink: Callable[[CaptureOutcome], None],
    ) -> None:
        if type(reservation) is not CaptureReservation:
            raise ValueError("reservation must be an exact CaptureReservation")
        if not callable(sink):
            raise ValueError("sink must be callable")
        with self._lock:
            if self._lifecycle is not _Lifecycle.OPEN:
                raise RuntimeError("managed Resident capture port is closing")
            entry = self._matching_entry(reservation)
            if entry.state is not _ReservationState.RESERVED_UNREGISTERED:
                raise ValueError("reservation is already registered or terminal")
            entry.state = _ReservationState.REGISTERING

        try:
            completion = entry.target.pool.capture_completion(entry.binding)
            waiter = threading.Thread(
                target=self._wait_for_completion,
                args=(reservation.reservation_id, completion),
                name=f"managed-resident-capture-{reservation.reservation_id}",
                daemon=False,
            )
        except Exception:
            self._registration_failed(entry)
            raise

        start_error: Exception | None = None
        with self._lock:
            current = self._reservations.get(reservation.reservation_id)
            if current is not entry or current.state not in {
                _ReservationState.REGISTERING,
                _ReservationState.REGISTERING_CANCEL_REQUESTED,
            }:
                raise RuntimeError("registration ownership was lost")
            entry.completion = completion
            entry.sink = sink
            entry.waiter = waiter
            try:
                waiter.start()
            except Exception as exc:
                self._registration_failed_locked(entry)
                start_error = exc
            else:
                if current.state in {
                    _ReservationState.REGISTERING,
                    _ReservationState.REGISTERING_CANCEL_REQUESTED,
                }:
                    current.state = _ReservationState.REGISTERED_WAITING
                self._condition.notify_all()
        if start_error is not None:
            raise start_error

    def start(
        self,
        reservation: CaptureReservation,
        trigger_monotonic_ns: int,
        target_frame_count: int,
    ) -> None:
        if type(reservation) is not CaptureReservation:
            raise ValueError("reservation must be an exact CaptureReservation")
        with self._lock:
            if self._lifecycle is not _Lifecycle.OPEN:
                raise RuntimeError("managed Resident capture port is closing")
            entry = self._matching_entry(reservation)
            if (
                entry.state is not _ReservationState.REGISTERED_WAITING
                or entry.start_claimed
            ):
                raise ValueError("reservation is not ready to start")
            completion = entry.completion
            entry.start_claimed = True
            entry.start_in_flight = True

        try:
            returned = entry.target.pool.start_capture(
                entry.binding,
                trigger_monotonic_ns=trigger_monotonic_ns,
                target_frame_count=target_frame_count,
            )
        except Exception:
            settled_at = time.monotonic()
            with self._lock:
                self._settle_start_locked(entry, False, settled_at)
            raise
        settled_at = time.monotonic()
        with self._lock:
            self._settle_start_locked(
                entry,
                returned is completion,
                settled_at,
            )
        if returned is not completion:
            raise RuntimeError("start_capture returned another completion")

    def cancel(
        self,
        reservation: CaptureReservation,
    ) -> ResidentCancelResult:
        if type(reservation) is not CaptureReservation:
            raise ValueError("reservation must be an exact CaptureReservation")
        with self._lock:
            entry = self._reservations.get(reservation.reservation_id)
            if entry is None or entry.reservation != reservation:
                return ResidentCancelResult(
                    status=ResidentCancelStatus.INVALID,
                    run_id=reservation.run_id,
                    reservation_id=reservation.reservation_id,
                    reason="reservation is unknown or retired",
                )
            if entry.cancel_requested:
                return ResidentCancelResult(
                    status=ResidentCancelStatus.INVALID,
                    run_id=reservation.run_id,
                    reservation_id=reservation.reservation_id,
                    reason="reservation cancel was already requested",
                )
            no_waiter = False
            if entry.state in {
                _ReservationState.RESERVED_UNREGISTERED,
                _ReservationState.REGISTRATION_FAILED_AWAITING_CANCEL,
            }:
                entry.state = _ReservationState.CANCELING_UNREGISTERED
                no_waiter = True
            elif entry.state is _ReservationState.REGISTERING:
                entry.state = _ReservationState.REGISTERING_CANCEL_REQUESTED
            elif entry.state not in {
                _ReservationState.REGISTERED_WAITING,
                _ReservationState.STARTED,
            }:
                status = (
                    ResidentCancelStatus.ALREADY_TERMINAL
                    if entry.state is _ReservationState.TERMINAL_DELIVERING
                    else ResidentCancelStatus.INVALID
                )
                return ResidentCancelResult(
                    status=status,
                    run_id=reservation.run_id,
                    reservation_id=reservation.reservation_id,
                    reason=(
                        ""
                        if status is ResidentCancelStatus.ALREADY_TERMINAL
                        else "reservation cannot be cancelled in its current state"
                    ),
                )
            entry.cancel_requested = True

        mapped, clean = self._call_cancel(entry)
        self._record_cancel_settlement(entry, clean, no_waiter=no_waiter)
        return mapped

    def close_and_join(
        self,
        timeout_seconds: float,
    ) -> ManagedResidentCaptureCloseReport:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= _MAX_CLOSE_SECONDS
        ):
            raise ValueError("timeout_seconds must be finite positive bounded seconds")
        deadline = time.monotonic() + timeout_seconds
        cancel_entries: list[tuple[_ReservationEntry, bool]] = []
        late_cancel_ids: set[str] = set()
        deadline_exceeded = False
        with self._lock:
            if self._clean_close_report is not None:
                return self._clean_close_report
            if self._lifecycle is _Lifecycle.OPEN:
                self._lifecycle = _Lifecycle.CLOSING
            for entry in tuple(self._reservations.values()):
                if entry.cancel_requested or entry.state in {
                    _ReservationState.NON_CLEAN,
                    _ReservationState.TERMINAL_DELIVERING,
                    _ReservationState.CANCELING_UNREGISTERED,
                    _ReservationState.CLOSE_CANCELING,
                }:
                    continue
                no_waiter = False
                if entry.state in {
                    _ReservationState.RESERVED_UNREGISTERED,
                    _ReservationState.REGISTRATION_FAILED_AWAITING_CANCEL,
                }:
                    entry.state = _ReservationState.CLOSE_CANCELING
                    no_waiter = True
                elif entry.state is _ReservationState.REGISTERING:
                    entry.state = _ReservationState.REGISTERING_CANCEL_REQUESTED
                    entry.close_requested = True
                elif entry.state not in {
                    _ReservationState.REGISTERING_CANCEL_REQUESTED,
                    _ReservationState.REGISTERED_WAITING,
                    _ReservationState.STARTED,
                }:
                    continue
                entry.cancel_requested = True
                entry.close_requested = True
                cancel_entries.append((entry, no_waiter))
            for run_id, claim in tuple(self._reserve_claims.items()):
                if claim.token is not None:
                    claim.cancel_requested = True
                    if not claim.in_flight:
                        claim.settled_at = time.monotonic()
                        del self._reserve_claims[run_id]
            observed_claims = tuple(self._reserve_claims.values())
            observed_start_entries = tuple(
                entry
                for entry in self._reservations.values()
                if entry.start_in_flight
            )

        for entry, no_waiter in cancel_entries:
            _, clean = self._call_cancel(entry)
            cancel_returned_at = time.monotonic()
            self._record_cancel_settlement(
                entry,
                clean,
                no_waiter=no_waiter,
            )
            if cancel_returned_at > deadline:
                late_cancel_ids.add(entry.reservation.reservation_id)
                deadline_exceeded = True

        with self._lock:
            if not deadline_exceeded:
                while any(
                    entry.state in {
                        _ReservationState.REGISTERING,
                        _ReservationState.REGISTERING_CANCEL_REQUESTED,
                    }
                    for entry in self._reservations.values()
                ) or any(
                    claim.in_flight for claim in self._reserve_claims.values()
                ) or any(
                    entry.start_in_flight
                    for entry in self._reservations.values()
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
            late_claim_ids = {
                claim.report_id
                for claim in observed_claims
                if (
                    claim.settled_at is not None
                    and claim.settled_at > deadline
                )
            }
            if late_claim_ids:
                late_cancel_ids.update(late_claim_ids)
                deadline_exceeded = True
            late_start_ids = {
                entry.reservation.reservation_id
                for entry in observed_start_entries
                if (
                    entry.start_settled_at is not None
                    and entry.start_settled_at > deadline
                )
            }
            if late_start_ids:
                late_cancel_ids.update(late_start_ids)
                deadline_exceeded = True
            waiters = (
                ()
                if deadline_exceeded
                else tuple(
                    entry.waiter
                    for entry in self._reservations.values()
                    if entry.waiter is not None
                )
            )

        for waiter in waiters:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            waiter.join(remaining)

        with self._lock:
            registration_ids = tuple(
                sorted(
                    reservation_id
                    for reservation_id, entry in self._reservations.items()
                    if entry.state
                    in {
                        _ReservationState.REGISTERING,
                        _ReservationState.REGISTERING_CANCEL_REQUESTED,
                    }
                )
            )
            waiter_ids = tuple(
                sorted(
                    reservation_id
                    for reservation_id, entry in self._reservations.items()
                    if entry.waiter is not None and entry.waiter.is_alive()
                )
            )
            reservation_ids = tuple(
                sorted(
                    set(self._reservations).union(
                        claim.report_id
                        for claim in self._reserve_claims.values()
                    ).union(late_cancel_ids)
                )
            )
            if not reservation_ids:
                self._lifecycle = _Lifecycle.CLOSED
                report = ManagedResidentCaptureCloseReport(
                    status=ManagedResidentCaptureCloseStatus.CLOSED,
                    remaining_registration_ids=(),
                    remaining_reservation_ids=(),
                    remaining_waiter_ids=(),
                )
                self._clean_close_report = report
                return report
            return ManagedResidentCaptureCloseReport(
                status=ManagedResidentCaptureCloseStatus.NON_CLEAN,
                remaining_registration_ids=registration_ids,
                remaining_reservation_ids=reservation_ids,
                remaining_waiter_ids=waiter_ids,
            )

    def _wait_for_completion(
        self,
        reservation_id: str,
        completion: object,
    ) -> None:
        mapped: CaptureOutcome
        try:
            completion.wait(None)  # type: ignore[attr-defined]
            with self._lock:
                entry = self._reservations.get(reservation_id)
                if entry is None or entry.completion is not completion:
                    return
                pool = entry.target.pool
                binding = entry.binding
            barrier_completion = pool.capture_completion(binding)
            if barrier_completion is not completion:
                raise RuntimeError("completion barrier returned another completion")
            resident_outcome = completion.result_if_ready()  # type: ignore[attr-defined]
            mapped = self._map_outcome(entry, resident_outcome)
        except Exception as exc:
            with self._lock:
                entry = self._reservations.get(reservation_id)
                if entry is None:
                    return
            mapped = self._failed_outcome(
                entry,
                _bounded_reason(exc, "Resident completion failed"),
            )

        with self._lock:
            current = self._reservations.get(reservation_id)
            if current is not entry or current.state not in {
                _ReservationState.REGISTERED_WAITING,
                _ReservationState.STARTED,
            }:
                return
            current.state = _ReservationState.TERMINAL_DELIVERING
            sink = current.sink
            self._condition.notify_all()

        try:
            if sink is not None:
                sink(mapped)
        except Exception as exc:
            _LOGGER.exception(
                "completion_sink/callback_failed %s: %s",
                type(exc).__name__,
                _bounded_reason(exc, "completion sink failed"),
            )
        finally:
            with self._lock:
                current = self._reservations.get(reservation_id)
                if current is entry:
                    current.state = _ReservationState.RETIRED
                    if not current.start_in_flight:
                        del self._reservations[reservation_id]
                    self._condition.notify_all()

    def _settle_start_locked(
        self,
        entry: _ReservationEntry,
        succeeded: bool,
        settled_at: float,
    ) -> None:
        current = self._reservations.get(entry.reservation.reservation_id)
        if current is not entry:
            return
        current.start_in_flight = False
        current.start_settled_at = settled_at
        if current.state is _ReservationState.RETIRED:
            del self._reservations[entry.reservation.reservation_id]
        elif succeeded and current.state is _ReservationState.REGISTERED_WAITING:
            current.state = _ReservationState.STARTED
        self._condition.notify_all()

    def _registration_failed(self, entry: _ReservationEntry) -> None:
        with self._lock:
            self._registration_failed_locked(entry)

    def _registration_failed_locked(self, entry: _ReservationEntry) -> None:
        current = self._reservations.get(entry.reservation.reservation_id)
        if current is not entry:
            return
        current.completion = None
        current.sink = None
        current.waiter = None
        if current.cancel_settlement == "CLEAN":
            current.state = _ReservationState.RETIRED
            del self._reservations[entry.reservation.reservation_id]
        elif current.cancel_settlement == "NON_CLEAN":
            current.state = _ReservationState.NON_CLEAN
        else:
            current.state = (
                _ReservationState.REGISTRATION_FAILED_AWAITING_CANCEL
            )
        self._condition.notify_all()

    def _call_cancel(
        self,
        entry: _ReservationEntry,
    ) -> tuple[ResidentCancelResult, bool]:
        try:
            resident_result = entry.target.pool.cancel_capture(entry.binding)
            return self._map_cancel_result(entry, resident_result), True
        except Exception as exc:
            return (
                ResidentCancelResult(
                    status=ResidentCancelStatus.INVALID,
                    run_id=entry.reservation.run_id,
                    reservation_id=entry.reservation.reservation_id,
                    reason=_bounded_reason(exc, "Resident cancel failed"),
                ),
                False,
            )

    @staticmethod
    def _map_cancel_result(
        entry: _ReservationEntry,
        result: object,
    ) -> ResidentCancelResult:
        if not ManagedResidentCapturePort._resident_cancel_is_coherent(
            entry,
            result,
        ):
            raise TypeError("cancel_capture returned a malformed result")
        if result.status in {CancelStatus.CANCELLED, CancelStatus.ALREADY_TERMINAL}:
            if result.outcome is None or result.outcome.binding is not entry.binding:
                raise ValueError("cancel_capture returned a mismatched outcome")
            status = (
                ResidentCancelStatus.CANCELLED
                if result.status is CancelStatus.CANCELLED
                else ResidentCancelStatus.ALREADY_TERMINAL
            )
            reason = ""
        elif (
            result.status is CancelStatus.INVALID
            and result.error is not None
        ):
            status = ResidentCancelStatus.INVALID
            reason = _bounded_reason(
                result.error.summary,
                "Resident rejected capture cancellation",
            )
        else:
            raise ValueError("cancel_capture returned an incoherent result")
        return ResidentCancelResult(
            status=status,
            run_id=entry.reservation.run_id,
            reservation_id=entry.reservation.reservation_id,
            reason=reason,
        )

    @staticmethod
    def _resident_error_is_coherent(error: object) -> bool:
        if (
            type(error) is not ResidentError
            or type(error.code) is not ResidentErrorCode
        ):
            return False
        return type(error.summary) is str and bool(error.summary)

    @staticmethod
    def _resident_outcome_is_coherent(
        entry: _ReservationEntry,
        outcome: object,
    ) -> bool:
        if (
            type(outcome) is not ResidentCaptureOutcome
            or type(outcome.status) is not ResidentCaptureOutcomeStatus
            or outcome.binding is not entry.binding
        ):
            return False
        if outcome.status is ResidentCaptureOutcomeStatus.COMPLETED:
            return (
                type(outcome.audio) is CapturedAudio
                and outcome.audio.binding is entry.binding
                and outcome.error is None
            )
        if outcome.status is ResidentCaptureOutcomeStatus.CANCELLED:
            return (
                outcome.audio is None
                and ManagedResidentCapturePort._resident_error_is_coherent(
                    outcome.error
                )
                and outcome.error.code is ResidentErrorCode.CANCELLED
            )
        return (
            outcome.status is ResidentCaptureOutcomeStatus.FAILED
            and outcome.audio is None
            and ManagedResidentCapturePort._resident_error_is_coherent(
                outcome.error
            )
            and outcome.error.code is not ResidentErrorCode.CANCELLED
        )

    @staticmethod
    def _resident_cancel_is_coherent(
        entry: _ReservationEntry,
        result: object,
    ) -> bool:
        if (
            type(result) is not CancelResult
            or type(result.status) is not CancelStatus
        ):
            return False
        if result.status is CancelStatus.INVALID:
            return (
                result.outcome is None
                and ManagedResidentCapturePort._resident_error_is_coherent(
                    result.error
                )
                and result.error.code is ResidentErrorCode.INVALID
            )
        return (
            result.error is None
            and ManagedResidentCapturePort._resident_outcome_is_coherent(
                entry,
                result.outcome,
            )
            and (
                result.status is CancelStatus.ALREADY_TERMINAL
                or (
                    result.status is CancelStatus.CANCELLED
                    and result.outcome.status
                    is ResidentCaptureOutcomeStatus.CANCELLED
                )
            )
        )

    def _record_cancel_settlement(
        self,
        entry: _ReservationEntry,
        clean: bool,
        *,
        no_waiter: bool,
    ) -> None:
        with self._lock:
            current = self._reservations.get(entry.reservation.reservation_id)
            if current is not entry:
                return
            current.cancel_settlement = "CLEAN" if clean else "NON_CLEAN"
            if no_waiter or current.state in {
                _ReservationState.CANCELING_UNREGISTERED,
                _ReservationState.CLOSE_CANCELING,
                _ReservationState.REGISTRATION_FAILED_AWAITING_CANCEL,
            }:
                if clean:
                    current.state = _ReservationState.RETIRED
                    del self._reservations[entry.reservation.reservation_id]
                else:
                    current.state = _ReservationState.NON_CLEAN
            self._condition.notify_all()

    @staticmethod
    def _map_outcome(
        entry: _ReservationEntry,
        outcome: object,
    ) -> CaptureOutcome:
        if not ManagedResidentCapturePort._resident_outcome_is_coherent(
            entry,
            outcome,
        ):
            return ManagedResidentCapturePort._failed_outcome(
                entry,
                "Resident returned a malformed capture outcome",
            )
        if outcome.status is ResidentCaptureOutcomeStatus.COMPLETED:
            try:
                audio = ManagedResidentCapturePort._map_completed_audio(
                    entry,
                    outcome.audio,
                )
            except Exception as exc:
                return ManagedResidentCapturePort._failed_outcome(
                    entry,
                    _bounded_reason(exc, "Resident returned malformed audio"),
                )
            return CaptureOutcome(
                run_id=entry.reservation.run_id,
                reservation=entry.reservation,
                status=CaptureOutcomeStatus.COMPLETED,
                audio=audio,
                error_category=None,
                reason="",
            )
        if (
            outcome.status is ResidentCaptureOutcomeStatus.CANCELLED
            and outcome.audio is None
            and outcome.error is not None
        ):
            return CaptureOutcome(
                run_id=entry.reservation.run_id,
                reservation=entry.reservation,
                status=CaptureOutcomeStatus.CANCELLED,
                audio=None,
                error_category=TerminalErrorCategory.CAPTURE_CANCELLED,
                reason=_bounded_reason(
                    outcome.error.summary,
                    "Resident capture cancelled",
                ),
            )
        if (
            outcome.status is ResidentCaptureOutcomeStatus.FAILED
            and outcome.audio is None
            and outcome.error is not None
        ):
            return ManagedResidentCapturePort._failed_outcome(
                entry,
                _bounded_reason(outcome.error.summary, "Resident capture failed"),
            )
        return ManagedResidentCapturePort._failed_outcome(
            entry,
            "Resident returned a malformed capture outcome",
        )

    @staticmethod
    def _map_completed_audio(
        entry: _ReservationEntry,
        audio: object,
    ) -> CapturedAudioValue:
        if type(audio) is not CapturedAudio:
            raise TypeError("completed outcome has no exact CapturedAudio")
        samples = audio.samples
        channel_count = len(audio.channel_indices)
        if (
            audio.binding is not entry.binding
            or audio.run_id != entry.binding.run_id
            or audio.hardware_id != entry.binding.hardware_id
            or type(audio.channel_indices) is not tuple
            or not audio.channel_indices
            or audio.channel_indices != tuple(sorted(set(audio.channel_indices)))
            or not hasattr(samples, "dtype")
            or samples.dtype.name != "float32"
            or samples.ndim != 2
            or not samples.flags.c_contiguous
            or samples.flags.writeable
            or not (
                (samples.flags.owndata and samples.base is None)
                or (
                    not samples.flags.owndata
                    and type(samples.base) is bytes
                )
            )
            or samples.shape != (audio.frame_count, channel_count)
        ):
            raise ValueError("completed Resident audio does not match its binding")
        interleaved_bytes = samples.tobytes(order="C")
        if len(interleaved_bytes) != audio.frame_count * channel_count * 4:
            raise ValueError("completed Resident audio byte length is invalid")
        return CapturedAudioValue(
            source_identity=entry.binding.binding_id,
            sample_rate_hz=audio.sample_rate_hz,
            channel_count=channel_count,
            frame_count=audio.frame_count,
            format=CapturedSourceFormat.FLOAT32_INTERLEAVED,
            interleaved_bytes=interleaved_bytes,
            channel_indices=audio.channel_indices,
        )

    @staticmethod
    def _failed_outcome(
        entry: _ReservationEntry,
        reason: str,
    ) -> CaptureOutcome:
        return CaptureOutcome(
            run_id=entry.reservation.run_id,
            reservation=entry.reservation,
            status=CaptureOutcomeStatus.FAILED,
            audio=None,
            error_category=TerminalErrorCategory.CAPTURE_FAILED,
            reason=_bounded_reason(reason, "Resident capture failed"),
        )

    def _complete_reserve_claim(
        self,
        claim: _ReserveClaim,
        outcome: object,
    ) -> CaptureReserveResult:
        token = claim.token
        if token is None:
            raise RuntimeError("two-phase reserve claim has no token")
        with self._lock:
            current = self._reserve_claims.get(token.run_id)
            if (
                current is not claim
                or claim.cancel_requested
                or self._lifecycle is not _Lifecycle.OPEN
            ):
                self._release_claim_locked(claim)
                return self._reserve_failure(
                    CaptureReserveStatus.CLOSING,
                    token.run_id,
                    token.hardware_id,
                    "lifecycle/closing: reserve claim was cancelled",
                )
        if type(outcome) is not LifecycleOutcome:
            self._release_claim(claim)
            return self._reserve_failure(
                CaptureReserveStatus.FAULT,
                token.run_id,
                token.hardware_id,
                "internal/invariant_violation: malformed readiness outcome",
            )
        if outcome.status is LifecycleStatus.GENERATION_CONTINUOUS:
            if type(outcome.generation_id) is not str or not outcome.generation_id:
                self._release_claim(claim)
                return self._reserve_failure(
                    CaptureReserveStatus.FAULT,
                    token.run_id,
                    token.hardware_id,
                    "internal/invariant_violation: readiness has no generation",
                )
            return self._reserve_pinned_generation(
                claim,
                outcome.generation_id,
            )

        status = (
            CaptureReserveStatus.CLOSING
            if outcome.status is LifecycleStatus.LIFECYCLE_CANCELLED
            else CaptureReserveStatus.NOT_READY
        )
        reason = self._lifecycle_reason(outcome)
        self._release_claim(claim)
        return self._reserve_failure(
            status,
            token.run_id,
            token.hardware_id,
            reason,
        )

    def _reserve_pinned_generation(
        self,
        claim: _ReserveClaim,
        generation_id: str,
    ) -> CaptureReserveResult:
        token = claim.token
        target = claim.target
        if token is None or target is None:
            raise RuntimeError("two-phase reserve claim is incomplete")
        try:
            binding = target.pool.reserve_capture(
                token.run_id,
                target.resident_hardware_id,
                generation_id=generation_id,
            )
            self._validate_binding(
                binding,
                token.run_id,
                target.resident_hardware_id,
            )
            reservation = CaptureReservation(
                reservation_id=binding.binding_id,
                run_id=token.run_id,
                hardware_id=token.hardware_id,
            )
        except Exception as exc:
            self._release_claim(claim)
            if (
                isinstance(exc, ResidentRequestError)
                and exc.error.code is ResidentErrorCode.NOT_READY
                and "generation changed" in exc.error.summary
            ):
                return self._reserve_failure(
                    CaptureReserveStatus.NOT_READY,
                    token.run_id,
                    token.hardware_id,
                    (
                        "publish_candidate/generation_replaced_for_trigger: "
                        f"{exc.error.summary}"
                    ),
                )
            return self._reserve_exception(
                exc,
                token.run_id,
                token.hardware_id,
            )

        entry = _ReservationEntry(
            reservation=reservation,
            target=target,
            binding=binding,
        )
        with self._lock:
            current = self._reserve_claims.get(token.run_id)
            if (
                current is claim
                and not claim.cancel_requested
                and self._lifecycle is _Lifecycle.OPEN
                and reservation.reservation_id not in self._reservations
            ):
                claim.in_flight = False
                claim.settled_at = time.monotonic()
                self._reservations[reservation.reservation_id] = entry
                del self._reserve_claims[token.run_id]
                self._condition.notify_all()
                return CaptureReserveResult(
                    status=CaptureReserveStatus.RESERVED,
                    run_id=token.run_id,
                    hardware_id=token.hardware_id,
                    reservation=reservation,
                    reason="",
                )
            claim.report_id = reservation.reservation_id
            late_status = (
                CaptureReserveStatus.CLOSING
                if (
                    claim.cancel_requested
                    or self._lifecycle is not _Lifecycle.OPEN
                )
                else CaptureReserveStatus.FAULT
            )
            self._condition.notify_all()

        _, clean = self._call_cancel(entry)
        with self._lock:
            current = self._reserve_claims.get(token.run_id)
            if current is claim:
                claim.in_flight = False
                claim.settled_at = time.monotonic()
                if clean:
                    del self._reserve_claims[token.run_id]
                else:
                    entry.state = _ReservationState.NON_CLEAN
                    entry.cancel_requested = True
                    entry.cancel_settlement = "NON_CLEAN"
                    self._reservations[reservation.reservation_id] = entry
                    del self._reserve_claims[token.run_id]
                self._condition.notify_all()
        return self._reserve_failure(
            late_status,
            token.run_id,
            token.hardware_id,
            (
                "lifecycle/closing: capture port closed during reservation"
                if late_status is CaptureReserveStatus.CLOSING
                else "internal/invariant_violation: duplicate Resident binding"
            ),
        )

    @staticmethod
    def _lifecycle_reason(outcome: LifecycleOutcome) -> str:
        diagnostic = outcome.diagnostic
        if diagnostic is None:
            return _bounded_reason(
                outcome.status.value,
                "Resident lifecycle unavailable",
            )
        return _bounded_reason(
            (
                f"{diagnostic.stage.value}/{diagnostic.reason_code.value}: "
                f"{diagnostic.detail}"
            ),
            "Resident lifecycle unavailable",
        )

    def _release_claim(self, claim: _ReserveClaim) -> bool:
        with self._lock:
            cancelled = (
                claim.cancel_requested
                or self._lifecycle is not _Lifecycle.OPEN
            )
            self._release_claim_locked(claim)
            return cancelled

    def _release_claim_locked(self, claim: _ReserveClaim) -> None:
        current = self._reserve_claims.get(claim.run_id)
        if current is claim:
            claim.in_flight = False
            claim.settled_at = time.monotonic()
            del self._reserve_claims[claim.run_id]
            self._condition.notify_all()

    def _run_has_capacity_owner_locked(self, run_id: str) -> bool:
        return run_id in self._reserve_claims or any(
            entry.reservation.run_id == run_id
            for entry in self._reservations.values()
        )

    def _matching_entry(
        self,
        reservation: CaptureReservation,
    ) -> _ReservationEntry:
        entry = self._reservations.get(reservation.reservation_id)
        if entry is None or entry.reservation != reservation:
            raise ValueError("reservation is unknown or mismatched")
        return entry

    @staticmethod
    def _validate_target(
        target: object,
        route: ManagedResidentCaptureRoute,
    ) -> None:
        if type(target) is not ResidentCaptureTarget:
            raise TypeError("capture_target returned a malformed target")
        if (
            target.generation != route.generation
            or type(target.resident_hardware_id) is not str
            or not target.resident_hardware_id
        ):
            raise ValueError("capture_target returned a mismatched target")

    @staticmethod
    def _validate_binding(
        binding: object,
        run_id: str,
        resident_hardware_id: str,
    ) -> None:
        if type(binding) is not CaptureBinding:
            raise TypeError("reserve_capture returned a malformed binding")
        if (
            type(binding.binding_id) is not str
            or not binding.binding_id
            or binding.run_id != run_id
            or binding.hardware_id != resident_hardware_id
        ):
            raise ValueError("reserve_capture returned a mismatched binding")

    @staticmethod
    def _reserve_failure(
        status: CaptureReserveStatus,
        run_id: str,
        hardware_id: str,
        reason: str,
    ) -> CaptureReserveResult:
        return CaptureReserveResult(
            status=status,
            run_id=run_id,
            hardware_id=hardware_id,
            reservation=None,
            reason=reason,
        )

    @classmethod
    def _reserve_exception(
        cls,
        exc: Exception,
        run_id: str,
        hardware_id: str,
    ) -> CaptureReserveResult:
        status = CaptureReserveStatus.NOT_READY
        if isinstance(exc, ResidentRequestError):
            status = {
                ResidentErrorCode.CAPACITY: CaptureReserveStatus.CAPACITY,
                ResidentErrorCode.NOT_READY: CaptureReserveStatus.NOT_READY,
                ResidentErrorCode.EXPIRED: CaptureReserveStatus.NOT_READY,
                ResidentErrorCode.CLOSING: CaptureReserveStatus.CLOSING,
            }.get(exc.error.code, CaptureReserveStatus.FAULT)
        return cls._reserve_failure(
            status,
            run_id,
            hardware_id,
            _bounded_reason(exc, "Resident capture reservation failed"),
        )
