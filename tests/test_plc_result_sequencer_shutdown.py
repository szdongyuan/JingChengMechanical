from __future__ import annotations

import logging
import threading

import pytest

from app.service.plc_result_sequencer import (
    PlcAttemptOutcome,
    PlcIngressTicket,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
    PlcResultAttemptReport,
    PlcResultSequencer,
    PlcShutdownClearFailure,
    PlcShutdownReport,
    PlcTestingStateAttemptReport,
    PlcTestingStateReason,
)
from tests.plc_result_sequencer_fakes import (
    BlockingResultPort,
    ImmediatelyBusyLock,
    ObservableLock,
    RaisingReportSink,
    RaisingTestingStatePort,
    RecordingPorts,
    SelectiveRaisingTestingStatePort,
)


def make_sequencer(
    ports: RecordingPorts | None = None,
    *,
    write_result=None,
    write_testing_state=None,
    report_attempt=None,
) -> tuple[PlcResultSequencer, RecordingPorts]:
    ports = ports or RecordingPorts()
    return (
        PlcResultSequencer(
            write_result=write_result or ports.write_result,
            write_testing_state=write_testing_state or ports.write_testing_state,
            report_attempt=report_attempt or ports.report_attempt,
        ),
        ports,
    )


def register(
    sequencer: PlcResultSequencer,
    run_id: str,
    plc_id: int = 10,
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


def shutdown_state_reports(reports: list[object]) -> list[PlcTestingStateAttemptReport]:
    return [
        report
        for report in reports
        if isinstance(report, PlcTestingStateAttemptReport)
        and report.reason_kind is PlcTestingStateReason.SHUTDOWN_CLEAR
    ]


@pytest.mark.parametrize(
    "timeout_seconds",
    [True, False, float("nan"), float("inf"), float("-inf"), -0.001],
)
def test_invalid_shutdown_timeout_does_not_start_closing(timeout_seconds: object):
    sequencer, ports = make_sequencer()

    with pytest.raises(ValueError):
        sequencer.shutdown(timeout_seconds)

    assert register(sequencer, "still-open").plc_sequence == 1
    assert ports.testing_calls == []


def test_large_finite_shutdown_timeout_is_supported():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "large-timeout")

    report = sequencer.shutdown(threading.TIMEOUT_MAX * 2)

    assert report == PlcShutdownReport(True, (10,), (), (), (ticket,))
    assert ports.testing_calls == [(10, False)]


def test_shutdown_atomically_closes_registration_and_all_normal_mutations():
    sequencer, ports = make_sequencer()
    accepted = register(sequencer, "accepted")
    pending_acceptance = register(sequencer, "pending-acceptance")
    pending_rejection = register(sequencer, "pending-rejection")
    pending_terminal = register(sequencer, "pending-terminal")
    assert sequencer.mark_accepted(accepted) is PlcMutationDisposition.APPLIED
    ports.testing_calls.clear()
    ports.reports.clear()

    report = sequencer.shutdown(1)

    assert report.clean is True
    assert sequencer.register(
        run_id="after-close",
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=10,
    ) == PlcRegistrationResult(PlcMutationDisposition.CLOSED, None)
    assert (
        sequencer.mark_accepted(pending_acceptance)
        is PlcMutationDisposition.CLOSED
    )
    assert (
        sequencer.publish_rejection(pending_rejection, {"value": "rejected"})
        is PlcMutationDisposition.CLOSED
    )
    assert (
        sequencer.publish_terminal(pending_terminal, {"value": "terminal"})
        is PlcMutationDisposition.CLOSED
    )
    assert (
        sequencer.publish_terminal(accepted, {"value": "late-terminal"})
        is PlcMutationDisposition.CLOSED
    )
    assert ports.testing_calls == [(10, False)]
    assert ports.result_calls == []


@pytest.mark.parametrize(
    "method_name",
    ["mark_accepted", "publish_rejection", "publish_terminal"],
)
def test_shutdown_returns_closed_for_well_formed_unknown_lane_ticket(
    method_name: str,
):
    sequencer, ports = make_sequencer()
    assert sequencer.shutdown(0) == PlcShutdownReport(True, (), (), (), ())
    unknown = PlcIngressTicket("unknown", 999, 1)

    if method_name == "mark_accepted":
        disposition = sequencer.mark_accepted(unknown)
    else:
        disposition = getattr(sequencer, method_name)(
            unknown,
            {"value": "must-not-write"},
        )

    assert disposition is PlcMutationDisposition.CLOSED
    assert sequencer.snapshot(999) is None
    assert sequencer._seen_run_ids == set()
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []


def test_idle_lanes_clear_once_in_stable_order_with_exact_success_reports():
    sequencer, ports = make_sequencer()
    tickets = [
        register(sequencer, "plc-30", 30),
        register(sequencer, "plc-10", 10),
        register(sequencer, "plc-20", 20),
    ]

    report = sequencer.shutdown(1)
    repeated = sequencer.shutdown(0)

    assert repeated is report
    assert report == PlcShutdownReport(
        clean=True,
        clear_returned_plc_ids=(10, 20, 30),
        clear_failures=(),
        skipped_busy_plc_ids=(),
        dropped_tickets=(tickets[1], tickets[2], tickets[0]),
    )
    assert ports.testing_calls == [(10, False), (20, False), (30, False)]
    assert shutdown_state_reports(ports.reports) == [
        PlcTestingStateAttemptReport(
            plc_id=plc_id,
            testing=False,
            reason_kind=PlcTestingStateReason.SHUTDOWN_CLEAR,
            outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
            reason=None,
        )
        for plc_id in (10, 20, 30)
    ]
    for plc_id in (10, 20, 30):
        snapshot = sequencer.snapshot(plc_id)
        assert snapshot is not None
        assert snapshot.closed is True
        assert snapshot.pending_sequences == ()


def test_shutdown_without_lanes_returns_clean_empty_frozen_report():
    sequencer, ports = make_sequencer()

    report = sequencer.shutdown(0)

    assert report == PlcShutdownReport(True, (), (), (), ())
    assert sequencer.shutdown(10) is report
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []


def test_pending_gap_slots_are_dropped_deterministically_without_result_calls():
    sequencer, ports = make_sequencer()
    plc_20 = [register(sequencer, f"20-{index}", 20) for index in range(1, 4)]
    plc_10 = [register(sequencer, f"10-{index}", 10) for index in range(1, 4)]
    assert (
        sequencer.publish_terminal(plc_20[2], {"value": "20-3"})
        is PlcMutationDisposition.APPLIED
    )
    assert (
        sequencer.publish_rejection(plc_20[1], {"value": "20-2"})
        is PlcMutationDisposition.APPLIED
    )
    assert (
        sequencer.publish_terminal(plc_10[2], {"value": "10-3"})
        is PlcMutationDisposition.APPLIED
    )

    report = sequencer.shutdown(1)

    assert report.clean is True
    assert report.clear_returned_plc_ids == (10, 20)
    assert report.clear_failures == ()
    assert report.skipped_busy_plc_ids == ()
    assert report.dropped_tickets == tuple(plc_10 + plc_20)
    assert ports.result_calls == []
    assert ports.testing_calls == [(10, False), (20, False)]


def test_shutdown_clear_failure_is_reported_once_with_the_same_exact_reason():
    ports = RecordingPorts()
    state_port = RaisingTestingStatePort(RuntimeError())
    sequencer, _ = make_sequencer(ports, write_testing_state=state_port)
    ticket = register(sequencer, "failed-clear")

    report = sequencer.shutdown(1)

    assert report == PlcShutdownReport(
        clean=False,
        clear_returned_plc_ids=(),
        clear_failures=(PlcShutdownClearFailure(10, "RuntimeError"),),
        skipped_busy_plc_ids=(),
        dropped_tickets=(ticket,),
    )
    assert state_port.calls == [(10, False)]
    assert shutdown_state_reports(ports.reports) == [
        PlcTestingStateAttemptReport(
            plc_id=10,
            testing=False,
            reason_kind=PlcTestingStateReason.SHUTDOWN_CLEAR,
            outcome=PlcAttemptOutcome.BOUNDARY_FAILED,
            reason="RuntimeError",
        )
    ]
    assert sequencer.shutdown(1) is report
    assert state_port.calls == [(10, False)]


def test_clear_failures_exclude_only_failed_plcs_and_preserve_stable_successes():
    ports = RecordingPorts()
    state_port = SelectiveRaisingTestingStatePort(
        {20: RuntimeError("clear unavailable")}
    )
    sequencer, _ = make_sequencer(ports, write_testing_state=state_port)
    register(sequencer, "plc-30", 30)
    register(sequencer, "plc-20", 20)
    register(sequencer, "plc-10", 10)

    report = sequencer.shutdown(1)

    assert state_port.calls == [(10, False), (20, False), (30, False)]
    assert report.clean is False
    assert report.clear_returned_plc_ids == (10, 30)
    assert report.clear_failures == (
        PlcShutdownClearFailure(20, "clear unavailable"),
    )
    assert report.skipped_busy_plc_ids == ()


def test_blocked_preclose_result_is_skipped_without_concurrent_or_late_clear():
    ports = RecordingPorts()
    result_port = BlockingResultPort()
    sequencer, _ = make_sequencer(ports, write_result=result_port)
    ticket = register(sequencer, "blocked")
    publication_results: list[PlcMutationDisposition] = []
    publication_thread = threading.Thread(
        target=lambda: publication_results.append(
            sequencer.publish_terminal(ticket, {"value": "blocked"})
        )
    )
    publication_thread.start()
    result_entered = result_port.entered.wait(timeout=2)
    if not result_entered:
        result_port.release.set()
        publication_thread.join(timeout=2)
        assert not publication_thread.is_alive()
    assert result_entered

    try:
        report = sequencer.shutdown(0.02)
        assert report.clean is False
        assert report.clear_returned_plc_ids == ()
        assert report.clear_failures == ()
        assert report.skipped_busy_plc_ids == (10,)
        assert report.dropped_tickets == ()
        assert ports.testing_calls == []
    finally:
        result_port.release.set()
        publication_thread.join(timeout=2)

    assert not publication_thread.is_alive()
    assert publication_results == [PlcMutationDisposition.APPLIED]
    assert ports.testing_calls == []
    assert sequencer.shutdown(1) is report


def test_preclose_result_finishes_before_deadline_then_shutdown_clears_false():
    ports = RecordingPorts()
    result_port = BlockingResultPort()
    sequencer, _ = make_sequencer(ports, write_result=result_port)
    ticket = register(sequencer, "finishes")
    publication_results: list[PlcMutationDisposition] = []
    shutdown_reports: list[PlcShutdownReport] = []
    publication_thread = threading.Thread(
        target=lambda: publication_results.append(
            sequencer.publish_terminal(ticket, {"value": "done"})
        )
    )
    publication_thread.start()
    result_entered = result_port.entered.wait(timeout=2)
    if not result_entered:
        result_port.release.set()
        publication_thread.join(timeout=2)
        assert not publication_thread.is_alive()
    assert result_entered
    shutdown_thread = threading.Thread(
        target=lambda: shutdown_reports.append(sequencer.shutdown(2))
    )
    shutdown_thread.start()
    closing_started = sequencer._closing.wait(timeout=2)
    try:
        result_port.release.set()
    finally:
        publication_thread.join(timeout=2)
        shutdown_thread.join(timeout=2)

    assert closing_started
    assert not publication_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert publication_results == [PlcMutationDisposition.APPLIED]
    assert shutdown_reports[0].clear_returned_plc_ids == (10,)
    assert ports.testing_calls == [(10, False)]
    assert [
        type(report) for report in ports.reports
    ] == [PlcResultAttemptReport, PlcTestingStateAttemptReport]
    assert shutdown_state_reports(ports.reports)[0].outcome is PlcAttemptOutcome.BOUNDARY_RETURNED


def test_waiting_normal_call_observes_close_and_cannot_write_after_final_false():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "waiting")
    observable_lock = ObservableLock()
    with sequencer._map_lock:
        sequencer._lanes[10].lock = observable_lock
    assert observable_lock.acquire()
    publication_results: list[PlcMutationDisposition] = []
    shutdown_reports: list[PlcShutdownReport] = []
    publication_thread = threading.Thread(
        target=lambda: publication_results.append(
            sequencer.publish_terminal(ticket, {"value": "late"})
        )
    )
    publication_thread.start()
    publication_waiting = observable_lock.second_acquire_entered.wait(timeout=2)
    if not publication_waiting:
        observable_lock.release()
        publication_thread.join(timeout=2)
        assert not publication_thread.is_alive()
    assert publication_waiting
    shutdown_thread = threading.Thread(
        target=lambda: shutdown_reports.append(sequencer.shutdown(1))
    )
    shutdown_thread.start()
    closing_started = sequencer._closing.wait(timeout=2)
    try:
        observable_lock.release()
    finally:
        publication_thread.join(timeout=2)
        shutdown_thread.join(timeout=2)

    assert closing_started
    assert not publication_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert publication_results == [PlcMutationDisposition.CLOSED]
    assert shutdown_reports[0].clear_returned_plc_ids == (10,)
    assert ports.testing_calls == [(10, False)]
    assert ports.result_calls == []


def test_busy_plc_a_does_not_prevent_idle_plc_b_clear_when_time_remains():
    sequencer, ports = make_sequencer()
    register(sequencer, "busy-a", 10)
    ticket_b = register(sequencer, "idle-b", 20)
    with sequencer._map_lock:
        sequencer._lanes[10].lock = ImmediatelyBusyLock()

    report = sequencer.shutdown(1)

    assert report.clean is False
    assert report.clear_returned_plc_ids == (20,)
    assert report.clear_failures == ()
    assert report.skipped_busy_plc_ids == (10,)
    assert report.dropped_tickets == (ticket_b,)
    assert ports.testing_calls == [(20, False)]


def test_concurrent_shutdown_callers_join_one_clear_and_receive_same_report():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "one-clear")
    observable_lock = ObservableLock()
    with sequencer._map_lock:
        sequencer._lanes[10].lock = observable_lock
    assert observable_lock.acquire()
    reports: list[PlcShutdownReport] = []
    owner_thread = threading.Thread(
        target=lambda: reports.append(sequencer.shutdown(2))
    )
    owner_thread.start()
    closing_started = sequencer._closing.wait(timeout=2)
    clear_waiting = observable_lock.second_acquire_entered.wait(timeout=2)
    if not closing_started or not clear_waiting:
        observable_lock.release()
        owner_thread.join(timeout=2)
        assert not owner_thread.is_alive()
    assert closing_started
    assert clear_waiting

    joiner_entered = threading.Event()
    joiner_done = threading.Event()

    def join_shutdown() -> None:
        joiner_entered.set()
        reports.append(sequencer.shutdown(0))
        joiner_done.set()

    joiner_thread = threading.Thread(target=join_shutdown)
    joiner_thread.start()
    try:
        assert joiner_entered.wait(timeout=2)
        assert not joiner_done.is_set()
        assert ports.testing_calls == []
    finally:
        observable_lock.release()
        owner_thread.join(timeout=2)
        joiner_thread.join(timeout=2)
        assert not owner_thread.is_alive()
        assert not joiner_thread.is_alive()

    assert len(reports) == 2
    assert reports[0] is reports[1]
    assert reports[0].dropped_tickets == (ticket,)
    assert sequencer.shutdown(10) is reports[0]
    assert ports.testing_calls == [(10, False)]


def test_shutdown_report_sink_failure_is_logged_and_does_not_change_success(
    caplog: pytest.LogCaptureFixture,
):
    ports = RecordingPorts()
    report_sink = RaisingReportSink(RuntimeError("sink unavailable"))
    sequencer, _ = make_sequencer(ports, report_attempt=report_sink)
    ticket = register(sequencer, "sink-failure")

    with caplog.at_level(logging.ERROR, logger="app.service.plc_result_sequencer"):
        report = sequencer.shutdown(1)

    assert report == PlcShutdownReport(True, (10,), (), (), (ticket,))
    assert ports.testing_calls == [(10, False)]
    assert len(report_sink.calls) == 1
    assert report_sink.calls[0] == PlcTestingStateAttemptReport(
        plc_id=10,
        testing=False,
        reason_kind=PlcTestingStateReason.SHUTDOWN_CLEAR,
        outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
        reason=None,
    )
    assert caplog.messages.count("PLC attempt report sink failed") == 1
    assert sequencer.shutdown(1) is report
    assert len(report_sink.calls) == 1


def test_register_and_shutdown_map_lock_race_has_only_linearizable_outcomes():
    for index in range(20):
        sequencer, ports = make_sequencer()
        start = threading.Barrier(3)
        registration_results: list[PlcRegistrationResult] = []
        shutdown_reports: list[PlcShutdownReport] = []

        def race_registration() -> None:
            start.wait(timeout=2)
            registration_results.append(
                sequencer.register(
                    run_id=f"race-{index}",
                    trigger_source="plc",
                    operation_kind=PlcOperationKind.INITIAL,
                    plc_id=10,
                )
            )

        def race_shutdown() -> None:
            start.wait(timeout=2)
            shutdown_reports.append(sequencer.shutdown(1))

        registration_thread = threading.Thread(target=race_registration)
        shutdown_thread = threading.Thread(target=race_shutdown)
        registration_thread.start()
        shutdown_thread.start()
        try:
            start.wait(timeout=2)
        finally:
            registration_thread.join(timeout=2)
            shutdown_thread.join(timeout=2)

        assert not registration_thread.is_alive()
        assert not shutdown_thread.is_alive()
        assert len(registration_results) == len(shutdown_reports) == 1
        registration_result = registration_results[0]
        shutdown_report = shutdown_reports[0]
        if registration_result.disposition is PlcMutationDisposition.APPLIED:
            ticket = registration_result.ticket
            assert ticket == PlcIngressTicket(f"race-{index}", 10, 1)
            assert shutdown_report == PlcShutdownReport(
                True,
                (10,),
                (),
                (),
                (ticket,),
            )
            assert sequencer.snapshot(10) is not None
            assert f"race-{index}" in sequencer._seen_run_ids
            assert ports.testing_calls == [(10, False)]
        else:
            assert registration_result == PlcRegistrationResult(
                PlcMutationDisposition.CLOSED,
                None,
            )
            assert shutdown_report == PlcShutdownReport(True, (), (), (), ())
            assert sequencer.snapshot(10) is None
            assert f"race-{index}" not in sequencer._seen_run_ids
            assert ports.testing_calls == []
