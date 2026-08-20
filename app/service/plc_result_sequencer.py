from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


PlcScalar: TypeAlias = None | bool | int | float | str

_LOGGER = logging.getLogger(__name__)


class PlcOperationKind(str, Enum):
    INITIAL = "initial"
    REANALYSIS = "reanalysis"
    MANUAL_JUDGMENT = "manual_judgment"


class PlcMutationDisposition(str, Enum):
    APPLIED = "applied"
    EXCLUDED = "excluded"
    DUPLICATE_OR_STALE = "duplicate_or_stale"
    INVALID_STATE = "invalid_state"
    CLOSED = "closed"


class PlcAttemptOutcome(str, Enum):
    BOUNDARY_RETURNED = "boundary_returned"
    BOUNDARY_FAILED = "boundary_failed"


@dataclass(frozen=True)
class PlcIngressTicket:
    run_id: str
    plc_id: int
    plc_sequence: int


@dataclass(frozen=True)
class PlcRegistrationResult:
    disposition: PlcMutationDisposition
    ticket: PlcIngressTicket | None


@dataclass(frozen=True)
class PlcResultAttemptReport:
    run_id: str
    plc_id: int
    plc_sequence: int
    outcome: PlcAttemptOutcome
    field_name: str | None
    reason: str | None


@dataclass(frozen=True)
class PlcLaneSnapshot:
    plc_id: int
    next_sequence: int
    next_to_write: int
    active_count: int
    pending_sequences: tuple[int, ...]
    closed: bool


@dataclass(frozen=True)
class PlcShutdownClearFailure:
    plc_id: int
    reason: str


@dataclass(frozen=True)
class PlcShutdownReport:
    clean: bool
    clear_returned_plc_ids: tuple[int, ...]
    clear_failures: tuple[PlcShutdownClearFailure, ...]
    skipped_busy_plc_ids: tuple[int, ...]
    dropped_tickets: tuple[PlcIngressTicket, ...]


class PlcResultBoundaryError(Exception):
    def __init__(self, field_name: str, reason: str) -> None:
        self.field_name = field_name
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Slot:
    ticket: PlcIngressTicket
    accepted: bool = False
    terminal_kind: str | None = None
    terminal_payload: dict[str, PlcScalar] | None = None


class _Lane:
    def __init__(self, plc_id: int) -> None:
        self.plc_id = plc_id
        self.lock = threading.Lock()
        self.next_sequence = 1
        self.next_to_write = 1
        self.active_count = 0
        self.slots: dict[int, _Slot] = {}
        self.pending_slots: queue.SimpleQueue[_Slot] = queue.SimpleQueue()
        self.closed = False


class PlcResultSequencer:
    def __init__(
        self,
        *,
        write_result: Callable[[int, Mapping[str, PlcScalar]], None],
        report_attempt: Callable[[PlcResultAttemptReport], None],
    ) -> None:
        for name, port in (
            ("write_result", write_result),
            ("report_attempt", report_attempt),
        ):
            if not callable(port):
                raise ValueError(f"{name} must be callable")

        self._write_result = write_result
        self._report_attempt = report_attempt
        self._map_lock = threading.Lock()
        self._lanes: dict[int, _Lane] = {}
        self._seen_run_ids: set[str] = set()
        self._closing = threading.Event()
        self._shutdown_condition = threading.Condition()
        self._shutdown_started = False
        self._shutdown_report: PlcShutdownReport | None = None

    def register(
        self,
        *,
        run_id: str,
        trigger_source: str,
        operation_kind: PlcOperationKind,
        plc_id: int | None,
    ) -> PlcRegistrationResult:
        _validate_nonempty_string(run_id, "run_id")
        _validate_nonempty_string(trigger_source, "trigger_source")
        if not isinstance(operation_kind, PlcOperationKind):
            raise ValueError("operation_kind must be a PlcOperationKind")

        if operation_kind is not PlcOperationKind.INITIAL or trigger_source != "plc":
            return PlcRegistrationResult(PlcMutationDisposition.EXCLUDED, None)

        _validate_plc_id(plc_id)

        with self._map_lock:
            if self._closing.is_set():
                return PlcRegistrationResult(PlcMutationDisposition.CLOSED, None)
            if run_id in self._seen_run_ids:
                return PlcRegistrationResult(
                    PlcMutationDisposition.INVALID_STATE,
                    None,
                )
            lane = self._lanes.get(plc_id)
            if lane is None:
                lane = _Lane(plc_id)
                self._lanes[plc_id] = lane
            return self._register_under_map_lock(lane, run_id)

    def snapshot(self, plc_id: int) -> PlcLaneSnapshot | None:
        _validate_plc_id(plc_id)
        with self._map_lock:
            lane = self._lanes.get(plc_id)
        if lane is None:
            return None

        with lane.lock:
            self._merge_pending_slots_locked(lane)
            return PlcLaneSnapshot(
                plc_id=lane.plc_id,
                next_sequence=lane.next_sequence,
                next_to_write=lane.next_to_write,
                active_count=lane.active_count,
                pending_sequences=tuple(sorted(lane.slots)),
                closed=lane.closed,
            )

    def shutdown(self, timeout_seconds: float) -> PlcShutdownReport:
        _validate_shutdown_timeout(timeout_seconds)

        with self._shutdown_condition:
            if self._shutdown_report is not None:
                return self._shutdown_report
            if self._shutdown_started:
                while self._shutdown_report is None:
                    self._shutdown_condition.wait()
                return self._shutdown_report
            self._shutdown_started = True

        report = self._perform_shutdown(float(timeout_seconds))
        with self._shutdown_condition:
            self._shutdown_report = report
            self._shutdown_condition.notify_all()
        return report

    def mark_accepted(
        self,
        ticket: PlcIngressTicket,
    ) -> PlcMutationDisposition:
        _validate_ticket(ticket)
        with self._map_lock:
            lane = self._lanes.get(ticket.plc_id)
            closing = self._closing.is_set()
        if lane is None:
            return (
                PlcMutationDisposition.CLOSED
                if closing
                else PlcMutationDisposition.DUPLICATE_OR_STALE
            )

        with lane.lock:
            self._merge_pending_slots_locked(lane)
            if self._closing.is_set():
                return PlcMutationDisposition.CLOSED
            if ticket.plc_sequence < lane.next_to_write:
                return PlcMutationDisposition.DUPLICATE_OR_STALE
            slot = lane.slots.get(ticket.plc_sequence)
            if slot is None:
                return PlcMutationDisposition.DUPLICATE_OR_STALE
            if slot.ticket != ticket:
                return PlcMutationDisposition.INVALID_STATE
            if slot.terminal_kind is not None:
                return PlcMutationDisposition.INVALID_STATE
            if slot.accepted:
                return PlcMutationDisposition.DUPLICATE_OR_STALE

            slot.accepted = True
            lane.active_count += 1
            return PlcMutationDisposition.APPLIED

    def publish_rejection(
        self,
        ticket: PlcIngressTicket,
        payload: Mapping[str, PlcScalar],
    ) -> PlcMutationDisposition:
        _validate_ticket(ticket)
        copied_payload = _copy_payload(payload)
        lane, closing = self._lane_for_ticket(ticket)
        if lane is None:
            return (
                PlcMutationDisposition.CLOSED
                if closing
                else PlcMutationDisposition.DUPLICATE_OR_STALE
            )

        with lane.lock:
            self._merge_pending_slots_locked(lane)
            disposition, slot = self._publication_slot(lane, ticket)
            if disposition is not None:
                return disposition
            assert slot is not None
            if slot.terminal_kind == "rejection":
                return PlcMutationDisposition.DUPLICATE_OR_STALE
            if slot.terminal_kind is not None or slot.accepted:
                return PlcMutationDisposition.INVALID_STATE

            slot.terminal_kind = "rejection"
            slot.terminal_payload = copied_payload
            self._drain_ready_results(lane)
            return PlcMutationDisposition.APPLIED

    def publish_terminal(
        self,
        ticket: PlcIngressTicket,
        payload: Mapping[str, PlcScalar],
    ) -> PlcMutationDisposition:
        _validate_ticket(ticket)
        copied_payload = _copy_payload(payload)
        lane, closing = self._lane_for_ticket(ticket)
        if lane is None:
            return (
                PlcMutationDisposition.CLOSED
                if closing
                else PlcMutationDisposition.DUPLICATE_OR_STALE
            )

        with lane.lock:
            self._merge_pending_slots_locked(lane)
            disposition, slot = self._publication_slot(lane, ticket)
            if disposition is not None:
                return disposition
            assert slot is not None
            if slot.terminal_kind == "terminal":
                return PlcMutationDisposition.DUPLICATE_OR_STALE
            if slot.terminal_kind is not None:
                return PlcMutationDisposition.INVALID_STATE

            slot.terminal_kind = "terminal"
            slot.terminal_payload = copied_payload
            if slot.accepted:
                lane.active_count -= 1
            self._drain_ready_results(lane)
            return PlcMutationDisposition.APPLIED

    def _register_under_map_lock(
        self,
        lane: _Lane,
        run_id: str,
    ) -> PlcRegistrationResult:
        sequence = lane.next_sequence
        ticket = PlcIngressTicket(run_id, lane.plc_id, sequence)
        lane.next_sequence += 1
        lane.pending_slots.put(_Slot(ticket))
        self._seen_run_ids.add(run_id)
        return PlcRegistrationResult(PlcMutationDisposition.APPLIED, ticket)

    @staticmethod
    def _merge_pending_slots_locked(lane: _Lane) -> None:
        while True:
            try:
                slot = lane.pending_slots.get_nowait()
            except queue.Empty:
                return
            sequence = slot.ticket.plc_sequence
            if sequence in lane.slots:
                raise RuntimeError("duplicate pending PLC sequence")
            lane.slots[sequence] = slot

    def _lane_for_ticket(
        self,
        ticket: PlcIngressTicket,
    ) -> tuple[_Lane | None, bool]:
        with self._map_lock:
            return self._lanes.get(ticket.plc_id), self._closing.is_set()

    def _publication_slot(
        self,
        lane: _Lane,
        ticket: PlcIngressTicket,
    ) -> tuple[PlcMutationDisposition | None, _Slot | None]:
        if self._closing.is_set():
            return PlcMutationDisposition.CLOSED, None
        if ticket.plc_sequence < lane.next_to_write:
            return PlcMutationDisposition.DUPLICATE_OR_STALE, None
        slot = lane.slots.get(ticket.plc_sequence)
        if slot is None:
            return PlcMutationDisposition.DUPLICATE_OR_STALE, None
        if slot.ticket != ticket:
            return PlcMutationDisposition.INVALID_STATE, None
        return None, slot

    def _drain_ready_results(self, lane: _Lane) -> None:
        while True:
            slot = lane.slots.get(lane.next_to_write)
            if slot is None or slot.terminal_kind is None:
                return
            assert slot.terminal_payload is not None

            try:
                self._write_result(lane.plc_id, slot.terminal_payload)
            except PlcResultBoundaryError as exc:
                outcome = PlcAttemptOutcome.BOUNDARY_FAILED
                field_name = exc.field_name
                reason = _exception_reason(exc)
            except Exception as exc:
                outcome = PlcAttemptOutcome.BOUNDARY_FAILED
                field_name = None
                reason = _exception_reason(exc)
            else:
                outcome = PlcAttemptOutcome.BOUNDARY_RETURNED
                field_name = None
                reason = None

            self._emit_report(
                PlcResultAttemptReport(
                    run_id=slot.ticket.run_id,
                    plc_id=slot.ticket.plc_id,
                    plc_sequence=slot.ticket.plc_sequence,
                    outcome=outcome,
                    field_name=field_name,
                    reason=reason,
                )
            )
            del lane.slots[lane.next_to_write]
            lane.next_to_write += 1

    def _perform_shutdown(self, timeout_seconds: float) -> PlcShutdownReport:
        with self._map_lock:
            self._closing.set()
            lanes = tuple(sorted(self._lanes.items()))

        deadline = time.monotonic() + timeout_seconds
        skipped_busy: list[int] = []
        dropped_tickets: list[PlcIngressTicket] = []

        for plc_id, lane in lanes:
            remaining = min(
                threading.TIMEOUT_MAX,
                max(0.0, deadline - time.monotonic()),
            )
            if not lane.lock.acquire(timeout=remaining):
                skipped_busy.append(plc_id)
                continue
            try:
                self._merge_pending_slots_locked(lane)
                lane.closed = True
                dropped_tickets.extend(
                    lane.slots[sequence].ticket
                    for sequence in sorted(lane.slots)
                )
                lane.slots.clear()
            finally:
                lane.lock.release()

        return PlcShutdownReport(
            clean=not skipped_busy,
            clear_returned_plc_ids=(),
            clear_failures=(),
            skipped_busy_plc_ids=tuple(skipped_busy),
            dropped_tickets=tuple(dropped_tickets),
        )

    def _emit_report(
        self,
        report: PlcResultAttemptReport,
    ) -> None:
        try:
            self._report_attempt(report)
        except Exception:
            _LOGGER.exception("PLC attempt report sink failed")


def _validate_nonempty_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


def _validate_plc_id(plc_id: object) -> None:
    if isinstance(plc_id, bool) or not isinstance(plc_id, int) or plc_id <= 0:
        raise ValueError("plc_id must be a positive integer")


def _validate_ticket(ticket: object) -> None:
    if not isinstance(ticket, PlcIngressTicket):
        raise ValueError("ticket must be a PlcIngressTicket")
    _validate_nonempty_string(ticket.run_id, "ticket.run_id")
    _validate_plc_id(ticket.plc_id)
    if (
        isinstance(ticket.plc_sequence, bool)
        or not isinstance(ticket.plc_sequence, int)
        or ticket.plc_sequence <= 0
    ):
        raise ValueError("ticket.plc_sequence must be a positive integer")


def _validate_shutdown_timeout(timeout_seconds: object) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
    ):
        raise ValueError("timeout_seconds must be finite and nonnegative")


def _copy_payload(payload: object) -> dict[str, PlcScalar]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")

    copied: dict[str, PlcScalar] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError("payload keys must be strings")
        if value is not None and type(value) not in (bool, int, float, str):
            raise ValueError("payload values must be flat PLC scalars")
        copied[key] = value
    return copied


def _exception_reason(exc: Exception) -> str:
    return str(exc) or type(exc).__name__
