from __future__ import annotations

import threading
import logging
from dataclasses import FrozenInstanceError, fields

import pytest

from app.service.plc_result_sequencer import (
    PlcAttemptOutcome,
    PlcIngressTicket,
    PlcLaneSnapshot,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
    PlcResultAttemptReport,
    PlcResultBoundaryError,
    PlcShutdownClearFailure,
    PlcShutdownReport,
    PlcTestingStateAttemptReport,
    PlcTestingStateReason,
    PlcResultSequencer,
)
from tests.plc_result_sequencer_fakes import (
    BlockingFalseTestingStatePort,
    BlockingTestingStatePort,
    ObservableLock,
    RaisingReportSink,
    RaisingTestingStatePort,
    RecordingPorts,
)


def make_sequencer(ports: RecordingPorts | None = None) -> tuple[PlcResultSequencer, RecordingPorts]:
    ports = ports or RecordingPorts()
    return (
        PlcResultSequencer(
            write_result=ports.write_result,
            write_testing_state=ports.write_testing_state,
            report_attempt=ports.report_attempt,
        ),
        ports,
    )


def register(
    sequencer: PlcResultSequencer,
    run_id: str,
    plc_id: int,
) -> PlcIngressTicket:
    result = sequencer.register(
        run_id=run_id,
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=plc_id,
    )
    assert result.disposition is PlcMutationDisposition.APPLIED
    assert result.ticket is not None
    return result.ticket


def test_public_enums_have_exact_string_values():
    assert [(item.name, item.value) for item in PlcOperationKind] == [
        ("INITIAL", "initial"),
        ("REANALYSIS", "reanalysis"),
        ("MANUAL_JUDGMENT", "manual_judgment"),
    ]
    assert [(item.name, item.value) for item in PlcMutationDisposition] == [
        ("APPLIED", "applied"),
        ("EXCLUDED", "excluded"),
        ("DUPLICATE_OR_STALE", "duplicate_or_stale"),
        ("INVALID_STATE", "invalid_state"),
        ("CLOSED", "closed"),
    ]
    assert [(item.name, item.value) for item in PlcAttemptOutcome] == [
        ("BOUNDARY_RETURNED", "boundary_returned"),
        ("BOUNDARY_FAILED", "boundary_failed"),
    ]
    assert [(item.name, item.value) for item in PlcTestingStateReason] == [
        ("ACTIVE_COUNT_TRANSITION", "active_count_transition"),
        ("SHUTDOWN_CLEAR", "shutdown_clear"),
    ]


@pytest.mark.parametrize(
    ("data_type", "field_names", "arguments"),
    [
        (PlcIngressTicket, ("run_id", "plc_id", "plc_sequence"), ("run", 1, 1)),
        (
            PlcRegistrationResult,
            ("disposition", "ticket"),
            (PlcMutationDisposition.APPLIED, PlcIngressTicket("run", 1, 1)),
        ),
        (
            PlcResultAttemptReport,
            ("run_id", "plc_id", "plc_sequence", "outcome", "field_name", "reason"),
            ("run", 1, 1, PlcAttemptOutcome.BOUNDARY_RETURNED, None, None),
        ),
        (
            PlcTestingStateAttemptReport,
            ("plc_id", "testing", "reason_kind", "outcome", "reason"),
            (
                1,
                True,
                PlcTestingStateReason.ACTIVE_COUNT_TRANSITION,
                PlcAttemptOutcome.BOUNDARY_RETURNED,
                None,
            ),
        ),
        (
            PlcLaneSnapshot,
            (
                "plc_id",
                "next_sequence",
                "next_to_write",
                "active_count",
                "pending_sequences",
                "closed",
            ),
            (1, 2, 1, 0, (1,), False),
        ),
        (PlcShutdownClearFailure, ("plc_id", "reason"), (1, "failed")),
        (
            PlcShutdownReport,
            (
                "clean",
                "clear_returned_plc_ids",
                "clear_failures",
                "skipped_busy_plc_ids",
                "dropped_tickets",
            ),
            (True, (), (), (), ()),
        ),
    ],
)
def test_public_dataclasses_have_exact_fields_and_are_frozen(
    data_type: type,
    field_names: tuple[str, ...],
    arguments: tuple[object, ...],
):
    assert tuple(field.name for field in fields(data_type)) == field_names
    value = data_type(*arguments)
    with pytest.raises(FrozenInstanceError):
        setattr(value, field_names[0], arguments[0])


def test_result_boundary_error_exposes_field_and_reason():
    error = PlcResultBoundaryError("res_score_offset", "unavailable")

    assert error.field_name == "res_score_offset"
    assert error.reason == "unavailable"


def test_registration_allocates_per_plc_monotonic_sequences():
    sequencer, ports = make_sequencer()

    tickets = [register(sequencer, f"run-{index}", 10) for index in range(1, 4)]
    other = register(sequencer, "other", 20)

    assert [ticket.plc_sequence for ticket in tickets] == [1, 2, 3]
    assert other.plc_sequence == 1
    assert sequencer.snapshot(10) == PlcLaneSnapshot(10, 4, 1, 0, (1, 2, 3), False)
    assert sequencer.snapshot(20) == PlcLaneSnapshot(20, 2, 1, 0, (1,), False)
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []


def test_concurrent_same_plc_registration_allocates_distinct_slots():
    sequencer, _ = make_sequencer()
    barrier = threading.Barrier(3)
    tickets: list[PlcIngressTicket] = []

    def register_after_barrier(run_id: str) -> None:
        barrier.wait(timeout=2)
        tickets.append(register(sequencer, run_id, 10))

    threads = [
        threading.Thread(target=register_after_barrier, args=(run_id,))
        for run_id in ("run-a", "run-b")
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(ticket.plc_sequence for ticket in tickets) == [1, 2]
    assert sequencer.snapshot(10) == PlcLaneSnapshot(10, 3, 1, 0, (1, 2), False)


@pytest.mark.parametrize(
    ("trigger_source", "operation_kind"),
    [
        ("manual", PlcOperationKind.INITIAL),
        ("scan", PlcOperationKind.INITIAL),
        ("imported", PlcOperationKind.INITIAL),
        ("plc", PlcOperationKind.REANALYSIS),
        ("plc", PlcOperationKind.MANUAL_JUDGMENT),
    ],
)
def test_non_plc_and_non_initial_registration_is_excluded(
    trigger_source: str,
    operation_kind: PlcOperationKind,
):
    sequencer, ports = make_sequencer()

    result = sequencer.register(
        run_id="excluded",
        trigger_source=trigger_source,
        operation_kind=operation_kind,
        plc_id=None,
    )

    assert result == PlcRegistrationResult(PlcMutationDisposition.EXCLUDED, None)
    assert sequencer.snapshot(1) is None
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("run_id", ""),
        ("run_id", None),
        ("run_id", 7),
        ("trigger_source", ""),
        ("trigger_source", None),
        ("trigger_source", 7),
        ("operation_kind", "initial"),
        ("operation_kind", None),
    ],
)
def test_invalid_common_registration_arguments_do_not_create_or_advance_lane(
    field_name: str,
    invalid_value: object,
):
    sequencer, _ = make_sequencer()
    arguments = {
        "run_id": "run",
        "trigger_source": "plc",
        "operation_kind": PlcOperationKind.INITIAL,
        "plc_id": 10,
    }
    arguments[field_name] = invalid_value

    with pytest.raises(ValueError):
        sequencer.register(**arguments)

    assert sequencer.snapshot(10) is None
    assert register(sequencer, "valid", 10).plc_sequence == 1


@pytest.mark.parametrize("plc_id", [None, True, False, 0, -1])
def test_invalid_plc_initial_id_does_not_create_or_advance_lane(plc_id: object):
    sequencer, _ = make_sequencer()

    with pytest.raises(ValueError):
        sequencer.register(
            run_id="invalid",
            trigger_source="plc",
            operation_kind=PlcOperationKind.INITIAL,
            plc_id=plc_id,
        )

    assert sequencer.snapshot(10) is None
    assert register(sequencer, "valid", 10).plc_sequence == 1


def test_duplicate_pending_run_id_is_invalid_without_advancing_sequence():
    sequencer, _ = make_sequencer()
    original = register(sequencer, "duplicate", 10)

    duplicate = sequencer.register(
        run_id=original.run_id,
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=10,
    )
    next_ticket = register(sequencer, "next", 10)

    assert duplicate == PlcRegistrationResult(PlcMutationDisposition.INVALID_STATE, None)
    assert next_ticket.plc_sequence == 2


def test_first_acceptance_increments_count_and_reports_successful_true():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "run-1", 10)

    disposition = sequencer.mark_accepted(ticket)

    assert disposition is PlcMutationDisposition.APPLIED
    assert sequencer.snapshot(10) == PlcLaneSnapshot(10, 2, 1, 1, (1,), False)
    assert ports.testing_calls == [(10, True)]
    assert ports.reports == [
        PlcTestingStateAttemptReport(
            plc_id=10,
            testing=True,
            reason_kind=PlcTestingStateReason.ACTIVE_COUNT_TRANSITION,
            outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
            reason=None,
        )
    ]


def test_second_same_plc_acceptance_increments_without_repeating_true():
    sequencer, ports = make_sequencer()
    first = register(sequencer, "run-1", 10)
    second = register(sequencer, "run-2", 10)

    assert sequencer.mark_accepted(first) is PlcMutationDisposition.APPLIED
    assert sequencer.mark_accepted(second) is PlcMutationDisposition.APPLIED

    assert sequencer.snapshot(10).active_count == 2
    assert ports.testing_calls == [(10, True)]
    assert len(ports.reports) == 1


def test_different_plcs_have_independent_counts_and_true_calls():
    sequencer, ports = make_sequencer()
    first = register(sequencer, "run-1", 10)
    second = register(sequencer, "run-2", 20)

    assert sequencer.mark_accepted(first) is PlcMutationDisposition.APPLIED
    assert sequencer.mark_accepted(second) is PlcMutationDisposition.APPLIED

    assert sequencer.snapshot(10).active_count == 1
    assert sequencer.snapshot(20).active_count == 1
    assert ports.testing_calls == [(10, True), (20, True)]


def test_duplicate_foreign_and_stale_acceptance_do_not_change_state():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "run-1", 10)
    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
    calls_before = (list(ports.testing_calls), list(ports.reports))

    duplicate = sequencer.mark_accepted(ticket)
    foreign = sequencer.mark_accepted(PlcIngressTicket("foreign", 10, 1))
    stale = sequencer.mark_accepted(PlcIngressTicket("stale", 10, 999))

    assert duplicate is PlcMutationDisposition.DUPLICATE_OR_STALE
    assert foreign is PlcMutationDisposition.INVALID_STATE
    assert stale is PlcMutationDisposition.DUPLICATE_OR_STALE
    assert sequencer.snapshot(10).active_count == 1
    assert (ports.testing_calls, ports.reports) == calls_before


@pytest.mark.parametrize(
    "invalid_ticket",
    [
        None,
        object(),
        PlcIngressTicket("", 10, 1),
        PlcIngressTicket("run", True, 1),
        PlcIngressTicket("run", 0, 1),
        PlcIngressTicket("run", 10, True),
        PlcIngressTicket("run", 10, 0),
    ],
)
def test_malformed_acceptance_ticket_raises_without_state_change(invalid_ticket: object):
    sequencer, ports = make_sequencer()
    registered = register(sequencer, "registered", 10)

    with pytest.raises(ValueError):
        sequencer.mark_accepted(invalid_ticket)

    assert sequencer.snapshot(10) == PlcLaneSnapshot(10, 2, 1, 0, (1,), False)
    assert registered.plc_sequence == 1
    assert ports.testing_calls == []
    assert ports.reports == []


def test_testing_state_failure_is_reported_once_without_rolling_back_count():
    ports = RecordingPorts()
    failing_state = RaisingTestingStatePort(RuntimeError("state unavailable"))
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=failing_state,
        report_attempt=ports.report_attempt,
    )
    ticket = register(sequencer, "run-1", 10)

    disposition = sequencer.mark_accepted(ticket)

    assert disposition is PlcMutationDisposition.APPLIED
    assert sequencer.snapshot(10).active_count == 1
    assert failing_state.calls == [(10, True)]
    assert ports.reports == [
        PlcTestingStateAttemptReport(
            plc_id=10,
            testing=True,
            reason_kind=PlcTestingStateReason.ACTIVE_COUNT_TRANSITION,
            outcome=PlcAttemptOutcome.BOUNDARY_FAILED,
            reason="state unavailable",
        )
    ]


def test_report_sink_failure_is_logged_without_escaping_or_rolling_back(
    caplog: pytest.LogCaptureFixture,
):
    ports = RecordingPorts()
    report_sink = RaisingReportSink(RuntimeError("sink unavailable"))
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=report_sink,
    )
    ticket = register(sequencer, "run-1", 10)

    with caplog.at_level(logging.ERROR, logger="app.service.plc_result_sequencer"):
        disposition = sequencer.mark_accepted(ticket)

    assert disposition is PlcMutationDisposition.APPLIED
    assert sequencer.snapshot(10).active_count == 1
    assert ports.testing_calls == [(10, True)]
    assert len(report_sink.calls) == 1
    assert caplog.messages.count("PLC attempt report sink failed") == 1


def test_snapshot_unknown_and_malformed_plc_ids():
    sequencer, _ = make_sequencer()

    assert sequencer.snapshot(999) is None
    for invalid_plc_id in (None, True, False, 0, -1, "1"):
        with pytest.raises(ValueError):
            sequencer.snapshot(invalid_plc_id)


def test_blocked_same_plc_io_does_not_block_same_or_other_plc_registration():
    ports = RecordingPorts()
    blocking_state = BlockingTestingStatePort()
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=blocking_state,
        report_attempt=ports.report_attempt,
    )
    accepted = register(sequencer, "accepted", 10)
    observable_lane_lock = ObservableLock()
    with sequencer._map_lock:
        sequencer._lanes[10].lock = observable_lane_lock

    acceptance_thread = threading.Thread(target=sequencer.mark_accepted, args=(accepted,))
    acceptance_thread.start()
    assert blocking_state.entered.wait(timeout=2)

    same_done = threading.Event()
    other_done = threading.Event()

    def register_same_plc() -> None:
        register(sequencer, "same", 10)
        same_done.set()

    def register_other_plc() -> None:
        register(sequencer, "other", 20)
        other_done.set()

    same_thread = threading.Thread(target=register_same_plc)
    same_thread.start()

    other_thread = threading.Thread(target=register_other_plc)
    other_thread.start()
    try:
        same_completed_while_lane_blocked = same_done.wait(timeout=2)
        other_completed_while_lane_blocked = other_done.wait(timeout=2)
    finally:
        blocking_state.release.set()
        for thread in (acceptance_thread, same_thread, other_thread):
            thread.join(timeout=2)

    assert all(
        not thread.is_alive()
        for thread in (acceptance_thread, same_thread, other_thread)
    )
    assert same_completed_while_lane_blocked
    assert other_completed_while_lane_blocked
    assert not observable_lane_lock.second_acquire_entered.is_set()
    assert sequencer.snapshot(10).pending_sequences == (1, 2)
    assert sequencer.snapshot(20).pending_sequences == (1,)


def test_rejection_is_valid_only_before_acceptance_and_does_not_change_count():
    sequencer, ports = make_sequencer()
    rejected = register(sequencer, "rejected", 10)
    accepted = register(sequencer, "accepted", 10)
    assert sequencer.mark_accepted(accepted) is PlcMutationDisposition.APPLIED

    assert sequencer.publish_rejection(rejected, {"result": "rejected"}) is PlcMutationDisposition.APPLIED
    assert (
        sequencer.publish_rejection(accepted, {"result": "wrong"})
        is PlcMutationDisposition.INVALID_STATE
    )

    assert sequencer.snapshot(10).active_count == 1
    assert ports.testing_calls == [(10, True)]
    assert ports.result_calls == [(10, {"result": "rejected"})]


def test_counted_terminals_decrement_once_and_false_only_on_one_to_zero():
    sequencer, ports = make_sequencer()
    first = register(sequencer, "run-1", 10)
    second = register(sequencer, "run-2", 10)
    assert sequencer.mark_accepted(first) is PlcMutationDisposition.APPLIED
    assert sequencer.mark_accepted(second) is PlcMutationDisposition.APPLIED

    assert sequencer.publish_terminal(first, {"result": "first"}) is PlcMutationDisposition.APPLIED
    assert sequencer.snapshot(10).active_count == 1
    assert ports.testing_calls == [(10, True)]

    assert sequencer.publish_terminal(second, {"result": "second"}) is PlcMutationDisposition.APPLIED
    assert sequencer.snapshot(10).active_count == 0
    assert ports.testing_calls == [(10, True), (10, False)]
    assert ports.reports[-1] == PlcResultAttemptReport(
        run_id="run-2",
        plc_id=10,
        plc_sequence=2,
        outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
        field_name=None,
        reason=None,
    )
    assert PlcTestingStateAttemptReport(
        plc_id=10,
        testing=False,
        reason_kind=PlcTestingStateReason.ACTIVE_COUNT_TRANSITION,
        outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
        reason=None,
    ) in ports.reports

    calls_before = (
        list(ports.testing_calls),
        list(ports.result_calls),
        list(ports.reports),
    )
    assert (
        sequencer.publish_terminal(second, {"result": "duplicate"})
        is PlcMutationDisposition.DUPLICATE_OR_STALE
    )
    assert sequencer.snapshot(10).active_count == 0
    assert (ports.testing_calls, ports.result_calls, ports.reports) == calls_before


def test_failed_normal_false_is_reported_once_and_count_remains_zero():
    ports = RecordingPorts()
    failing_state = RaisingTestingStatePort(RuntimeError("state unavailable"))
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=failing_state,
        report_attempt=ports.report_attempt,
    )
    ticket = register(sequencer, "run-1", 10)
    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
    ports.reports.clear()
    failing_state.calls.clear()

    assert sequencer.publish_terminal(ticket, {"result": "done"}) is PlcMutationDisposition.APPLIED

    assert sequencer.snapshot(10).active_count == 0
    assert failing_state.calls == [(10, False)]
    false_reports = [
        report
        for report in ports.reports
        if isinstance(report, PlcTestingStateAttemptReport)
    ]
    assert false_reports == [
        PlcTestingStateAttemptReport(
            plc_id=10,
            testing=False,
            reason_kind=PlcTestingStateReason.ACTIVE_COUNT_TRANSITION,
            outcome=PlcAttemptOutcome.BOUNDARY_FAILED,
            reason="state unavailable",
        )
    ]


def test_uncounted_durable_terminal_writes_without_testing_state_transition():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "capture-commit-failed", 10)

    assert sequencer.publish_terminal(ticket, {"result": "capture_failed"}) is PlcMutationDisposition.APPLIED

    assert sequencer.snapshot(10).active_count == 0
    assert ports.testing_calls == []
    assert ports.result_calls == [(10, {"result": "capture_failed"})]


@pytest.mark.parametrize(
    "invalid_ticket",
    [
        None,
        object(),
        PlcIngressTicket("", 10, 1),
        PlcIngressTicket("run", True, 1),
        PlcIngressTicket("run", 0, 1),
        PlcIngressTicket("run", 10, True),
        PlcIngressTicket("run", 10, 0),
    ],
)
@pytest.mark.parametrize("method_name", ["publish_rejection", "publish_terminal"])
def test_malformed_publication_ticket_raises_without_state_change(
    method_name: str,
    invalid_ticket: object,
):
    sequencer, ports = make_sequencer()
    registered = register(sequencer, "registered", 10)

    with pytest.raises(ValueError):
        getattr(sequencer, method_name)(invalid_ticket, {"result": "invalid"})

    assert sequencer.snapshot(10) == PlcLaneSnapshot(10, 2, 1, 0, (1,), False)
    assert registered.plc_sequence == 1
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []


def test_concurrent_false_then_true_is_serialized_in_that_order():
    ports = RecordingPorts()
    blocking_state = BlockingFalseTestingStatePort()
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=blocking_state,
        report_attempt=ports.report_attempt,
    )
    first = register(sequencer, "first", 10)
    second = register(sequencer, "second", 10)
    assert sequencer.mark_accepted(first) is PlcMutationDisposition.APPLIED
    observable_lane_lock = ObservableLock()
    with sequencer._map_lock:
        sequencer._lanes[10].lock = observable_lane_lock

    terminal_result: list[PlcMutationDisposition] = []
    acceptance_result: list[PlcMutationDisposition] = []
    terminal_thread = threading.Thread(
        target=lambda: terminal_result.append(
            sequencer.publish_terminal(first, {"result": "done"})
        )
    )
    terminal_thread.start()
    assert blocking_state.false_entered.wait(timeout=2)

    acceptance_started = threading.Barrier(2)

    def accept_second() -> None:
        acceptance_started.wait(timeout=2)
        acceptance_result.append(sequencer.mark_accepted(second))

    acceptance_thread = threading.Thread(target=accept_second)
    acceptance_thread.start()
    acceptance_started.wait(timeout=2)
    try:
        assert observable_lane_lock.second_acquire_entered.wait(timeout=2)
        assert blocking_state.calls == [(10, True), (10, False)]
    finally:
        blocking_state.release_false.set()
        terminal_thread.join(timeout=2)
        acceptance_thread.join(timeout=2)

    assert not terminal_thread.is_alive()
    assert not acceptance_thread.is_alive()
    assert terminal_result == [PlcMutationDisposition.APPLIED]
    assert acceptance_result == [PlcMutationDisposition.APPLIED]
    assert blocking_state.calls == [(10, True), (10, False), (10, True)]
