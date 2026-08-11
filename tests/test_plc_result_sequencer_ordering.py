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
    PlcResultBoundaryError,
    PlcResultSequencer,
)
from tests.plc_result_sequencer_fakes import (
    BlockingResultPort,
    ObservableLock,
    RaisingReportSink,
    RaisingResultPort,
    RecordingPorts,
)


def make_sequencer(
    ports: RecordingPorts | None = None,
    *,
    write_result=None,
    report_attempt=None,
) -> tuple[PlcResultSequencer, RecordingPorts]:
    ports = ports or RecordingPorts()
    return (
        PlcResultSequencer(
            write_result=write_result or ports.write_result,
            write_testing_state=ports.write_testing_state,
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


def result_reports(ports: RecordingPorts) -> list[PlcResultAttemptReport]:
    return [
        report
        for report in ports.reports
        if isinstance(report, PlcResultAttemptReport)
    ]


def test_out_of_order_terminals_drain_in_ingress_order():
    sequencer, ports = make_sequencer()
    tickets = [register(sequencer, f"run-{index}") for index in range(1, 4)]

    for ticket in reversed(tickets):
        assert (
            sequencer.publish_terminal(ticket, {"value": ticket.plc_sequence})
            is PlcMutationDisposition.APPLIED
        )

    assert ports.result_calls == [
        (10, {"value": 1}),
        (10, {"value": 2}),
        (10, {"value": 3}),
    ]
    assert [report.plc_sequence for report in result_reports(ports)] == [1, 2, 3]


def test_accepted_and_rejected_slots_mix_without_losing_ingress_order():
    sequencer, ports = make_sequencer()
    accepted_first = register(sequencer, "accepted-first")
    rejected_second = register(sequencer, "rejected-second")
    accepted_third = register(sequencer, "accepted-third")
    assert sequencer.mark_accepted(accepted_first) is PlcMutationDisposition.APPLIED
    assert sequencer.mark_accepted(accepted_third) is PlcMutationDisposition.APPLIED

    assert (
        sequencer.publish_terminal(accepted_third, {"value": 3})
        is PlcMutationDisposition.APPLIED
    )
    assert (
        sequencer.publish_rejection(rejected_second, {"value": 2})
        is PlcMutationDisposition.APPLIED
    )
    assert ports.result_calls == []
    assert (
        sequencer.publish_terminal(accepted_first, {"value": 1})
        is PlcMutationDisposition.APPLIED
    )

    assert ports.result_calls == [
        (10, {"value": 1}),
        (10, {"value": 2}),
        (10, {"value": 3}),
    ]


def test_preaccept_rejection_needs_no_durable_row_and_advances():
    sequencer, ports = make_sequencer()
    rejected = register(sequencer, "capacity-rejected")
    following = register(sequencer, "following")

    assert (
        sequencer.publish_terminal(following, {"value": 2})
        is PlcMutationDisposition.APPLIED
    )
    assert (
        sequencer.publish_rejection(rejected, {"value": 1})
        is PlcMutationDisposition.APPLIED
    )

    assert ports.testing_calls == []
    assert ports.result_calls == [(10, {"value": 1}), (10, {"value": 2})]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {1: "not-string-key"},
        {"nested": {}},
        {"nested": []},
        {"nested": set()},
        {"nested": object()},
    ],
)
@pytest.mark.parametrize("method_name", ["publish_rejection", "publish_terminal"])
def test_nonflat_payload_is_rejected_before_slot_mutation(method_name: str, payload: object):
    sequencer, ports = make_sequencer()
    gap = register(sequencer, "gap")
    ticket = register(sequencer, "target")

    with pytest.raises(ValueError):
        getattr(sequencer, method_name)(ticket, payload)

    assert sequencer.snapshot(10).pending_sequences == (1, 2)
    assert sequencer.snapshot(10).active_count == 0
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []
    assert sequencer.publish_rejection(gap, {"value": 1}) is PlcMutationDisposition.APPLIED
    assert sequencer.publish_terminal(ticket, {"value": 2}) is PlcMutationDisposition.APPLIED
    assert ports.result_calls == [(10, {"value": 1}), (10, {"value": 2})]


def test_flat_scalar_payload_is_copied_before_caller_mutation():
    sequencer, ports = make_sequencer()
    gap = register(sequencer, "gap")
    ticket = register(sequencer, "target")
    payload = {
        "none": None,
        "boolean": True,
        "integer": 7,
        "floating": 1.5,
        "text": "frozen",
    }

    assert sequencer.publish_terminal(ticket, payload) is PlcMutationDisposition.APPLIED
    payload["text"] = "mutated"
    payload["integer"] = 99
    assert sequencer.publish_rejection(gap, {"value": "gap"}) is PlcMutationDisposition.APPLIED

    assert ports.result_calls[1] == (
        10,
        {
            "none": None,
            "boolean": True,
            "integer": 7,
            "floating": 1.5,
            "text": "frozen",
        },
    )


def test_success_report_means_only_local_boundary_return():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "run-1")

    assert sequencer.publish_terminal(ticket, {"value": 1}) is PlcMutationDisposition.APPLIED

    assert result_reports(ports) == [
        PlcResultAttemptReport(
            run_id="run-1",
            plc_id=10,
            plc_sequence=1,
            outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
            field_name=None,
            reason=None,
        )
    ]


@pytest.mark.parametrize(
    ("error", "expected_field", "expected_reason"),
    [
        (
            PlcResultBoundaryError("res_score_offset", "unavailable"),
            "res_score_offset",
            "unavailable",
        ),
        (RuntimeError("transport unavailable"), None, "transport unavailable"),
        (RuntimeError(), None, "RuntimeError"),
    ],
)
def test_result_failure_reports_once_and_advances(
    error: Exception,
    expected_field: str | None,
    expected_reason: str,
):
    ports = RecordingPorts()
    result_port = RaisingResultPort({1: error})
    sequencer, _ = make_sequencer(ports, write_result=result_port)
    first = register(sequencer, "run-1")
    second = register(sequencer, "run-2")
    assert sequencer.publish_terminal(second, {"value": 2}) is PlcMutationDisposition.APPLIED

    assert sequencer.publish_terminal(first, {"value": 1}) is PlcMutationDisposition.APPLIED

    assert result_port.calls == [(10, {"value": 1}), (10, {"value": 2})]
    assert result_reports(ports) == [
        PlcResultAttemptReport(
            run_id="run-1",
            plc_id=10,
            plc_sequence=1,
            outcome=PlcAttemptOutcome.BOUNDARY_FAILED,
            field_name=expected_field,
            reason=expected_reason,
        ),
        PlcResultAttemptReport(
            run_id="run-2",
            plc_id=10,
            plc_sequence=2,
            outcome=PlcAttemptOutcome.BOUNDARY_RETURNED,
            field_name=None,
            reason=None,
        ),
    ]
    assert sequencer.snapshot(10).pending_sequences == ()
    assert sequencer.snapshot(10).next_to_write == 3


def test_result_report_sink_failure_is_contained_and_head_retires(
    caplog: pytest.LogCaptureFixture,
):
    ports = RecordingPorts()
    report_sink = RaisingReportSink(RuntimeError("sink unavailable"))
    sequencer, _ = make_sequencer(ports, report_attempt=report_sink)
    first = register(sequencer, "run-1")
    second = register(sequencer, "run-2")
    assert sequencer.publish_terminal(second, {"value": 2}) is PlcMutationDisposition.APPLIED

    with caplog.at_level(logging.ERROR, logger="app.service.plc_result_sequencer"):
        assert sequencer.publish_terminal(first, {"value": 1}) is PlcMutationDisposition.APPLIED

    assert ports.result_calls == [(10, {"value": 1}), (10, {"value": 2})]
    assert len(report_sink.calls) == 2
    assert caplog.messages.count("PLC attempt report sink failed") == 2
    assert sequencer.snapshot(10).pending_sequences == ()


def test_retained_rejection_lifecycle_dispositions_and_state_are_stable():
    sequencer, ports = make_sequencer()
    register(sequencer, "gap")
    ticket = register(sequencer, "rejected")
    payload = {"value": "rejected"}

    assert sequencer.publish_rejection(ticket, payload) is PlcMutationDisposition.APPLIED
    before = (sequencer.snapshot(10), list(ports.testing_calls), list(ports.result_calls))

    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.INVALID_STATE
    assert (
        sequencer.publish_terminal(ticket, {"value": "terminal"})
        is PlcMutationDisposition.INVALID_STATE
    )
    assert (
        sequencer.publish_rejection(ticket, {"value": "duplicate"})
        is PlcMutationDisposition.DUPLICATE_OR_STALE
    )

    assert (sequencer.snapshot(10), ports.testing_calls, ports.result_calls) == before
    assert payload == {"value": "rejected"}


def test_retained_accepted_terminal_lifecycle_dispositions_and_state_are_stable():
    sequencer, ports = make_sequencer()
    register(sequencer, "gap")
    ticket = register(sequencer, "accepted")
    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
    assert sequencer.publish_terminal(ticket, {"value": "terminal"}) is PlcMutationDisposition.APPLIED
    before = (
        sequencer.snapshot(10),
        list(ports.testing_calls),
        list(ports.result_calls),
        list(ports.reports),
    )

    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.INVALID_STATE
    assert (
        sequencer.publish_rejection(ticket, {"value": "rejection"})
        is PlcMutationDisposition.INVALID_STATE
    )
    assert (
        sequencer.publish_terminal(ticket, {"value": "duplicate"})
        is PlcMutationDisposition.DUPLICATE_OR_STALE
    )

    assert (
        sequencer.snapshot(10),
        ports.testing_calls,
        ports.result_calls,
        ports.reports,
    ) == before


def test_retained_accepted_slot_rejects_rejection_before_terminal():
    sequencer, ports = make_sequencer()
    register(sequencer, "gap")
    ticket = register(sequencer, "accepted")
    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
    before = (sequencer.snapshot(10), list(ports.testing_calls), list(ports.result_calls))

    assert (
        sequencer.publish_rejection(ticket, {"value": "rejection"})
        is PlcMutationDisposition.INVALID_STATE
    )

    assert (sequencer.snapshot(10), ports.testing_calls, ports.result_calls) == before


def test_duplicate_stale_and_field_mismatched_publications_do_not_repeat_calls():
    sequencer, ports = make_sequencer()
    ticket = register(sequencer, "run-1")
    assert sequencer.publish_terminal(ticket, {"value": 1}) is PlcMutationDisposition.APPLIED
    before = (list(ports.testing_calls), list(ports.result_calls), list(ports.reports))

    assert (
        sequencer.publish_terminal(ticket, {"value": "duplicate"})
        is PlcMutationDisposition.DUPLICATE_OR_STALE
    )
    assert (
        sequencer.publish_rejection(ticket, {"value": "stale"})
        is PlcMutationDisposition.DUPLICATE_OR_STALE
    )
    pending = register(sequencer, "run-2")
    foreign = PlcIngressTicket("foreign", pending.plc_id, pending.plc_sequence)
    assert (
        sequencer.publish_terminal(foreign, {"value": "foreign"})
        is PlcMutationDisposition.INVALID_STATE
    )
    assert (ports.testing_calls, ports.result_calls, ports.reports) == before


def test_duplicate_run_id_remains_invalid_after_original_slot_retires():
    sequencer, _ = make_sequencer()
    ticket = register(sequencer, "duplicate")
    assert sequencer.publish_terminal(ticket, {"value": 1}) is PlcMutationDisposition.APPLIED

    duplicate = sequencer.register(
        run_id="duplicate",
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=10,
    )
    next_ticket = register(sequencer, "next")

    assert duplicate == PlcRegistrationResult(PlcMutationDisposition.INVALID_STATE, None)
    assert next_ticket.plc_sequence == 2


def test_same_plc_second_payload_cannot_enter_while_first_is_blocked():
    ports = RecordingPorts()
    blocking_result = BlockingResultPort()
    sequencer, _ = make_sequencer(ports, write_result=blocking_result)
    first = register(sequencer, "run-1")
    second = register(sequencer, "run-2")
    observable_lane_lock = ObservableLock()
    with sequencer._map_lock:
        sequencer._lanes[10].lock = observable_lane_lock
    first_result: list[PlcMutationDisposition] = []
    second_result: list[PlcMutationDisposition] = []

    first_thread = threading.Thread(
        target=lambda: first_result.append(
            sequencer.publish_terminal(first, {"value": 1})
        )
    )
    first_thread.start()
    assert blocking_result.entered.wait(timeout=2)

    second_started = threading.Barrier(2)

    def publish_second() -> None:
        second_started.wait(timeout=2)
        second_result.append(sequencer.publish_terminal(second, {"value": 2}))

    second_thread = threading.Thread(target=publish_second)
    second_thread.start()
    second_started.wait(timeout=2)
    try:
        assert observable_lane_lock.second_acquire_entered.wait(timeout=2)
        assert blocking_result.calls == [(10, {"value": 1})]
    finally:
        blocking_result.release.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_result == [PlcMutationDisposition.APPLIED]
    assert second_result == [PlcMutationDisposition.APPLIED]
    assert blocking_result.calls == [(10, {"value": 1}), (10, {"value": 2})]


def test_same_plc_registration_does_not_wait_for_blocked_result_io():
    ports = RecordingPorts()
    blocking_result = BlockingResultPort()
    sequencer, _ = make_sequencer(ports, write_result=blocking_result)
    first = register(sequencer, "run-1")

    publisher = threading.Thread(
        target=lambda: sequencer.publish_terminal(first, {"value": 1})
    )
    publisher.start()
    assert blocking_result.entered.wait(timeout=2)

    registered = threading.Event()
    result = []

    def register_next() -> None:
        result.append(
            sequencer.register(
                run_id="run-2",
                trigger_source="plc",
                operation_kind=PlcOperationKind.INITIAL,
                plc_id=10,
            )
        )
        registered.set()

    registrar = threading.Thread(target=register_next)
    registrar.start()
    try:
        assert registered.wait(0.1), "PLC ingress waited for result I/O"
    finally:
        blocking_result.release.set()
        publisher.join(2)
        registrar.join(2)

    assert result == [
        PlcRegistrationResult(
            PlcMutationDisposition.APPLIED,
            PlcIngressTicket("run-2", 10, 2),
        )
    ]
    assert blocking_result.calls == [(10, {"value": 1})]


def test_blocked_plc_a_does_not_prevent_plc_b_completion():
    ports = RecordingPorts()
    blocking_result = BlockingResultPort(blocked_plc_id=10)
    sequencer, _ = make_sequencer(ports, write_result=blocking_result)
    plc_a = register(sequencer, "plc-a", 10)
    plc_b = register(sequencer, "plc-b", 20)

    plc_a_thread = threading.Thread(
        target=sequencer.publish_terminal,
        args=(plc_a, {"value": "a"}),
    )
    plc_a_thread.start()
    assert blocking_result.entered.wait(timeout=2)

    plc_b_done = threading.Event()

    def publish_plc_b() -> None:
        sequencer.publish_terminal(plc_b, {"value": "b"})
        plc_b_done.set()

    plc_b_thread = threading.Thread(target=publish_plc_b)
    plc_b_thread.start()
    try:
        assert plc_b_done.wait(timeout=2)
        assert (20, {"value": "b"}) in blocking_result.calls
    finally:
        blocking_result.release.set()
        plc_a_thread.join(timeout=2)
        plc_b_thread.join(timeout=2)

    assert not plc_a_thread.is_alive()
    assert not plc_b_thread.is_alive()


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
def test_excluded_operations_still_create_and_write_nothing(
    trigger_source: str,
    operation_kind: PlcOperationKind,
):
    sequencer, ports = make_sequencer()

    result = sequencer.register(
        run_id=f"{trigger_source}-{operation_kind.value}",
        trigger_source=trigger_source,
        operation_kind=operation_kind,
        plc_id=None,
    )

    assert result == PlcRegistrationResult(PlcMutationDisposition.EXCLUDED, None)
    assert sequencer.snapshot(10) is None
    assert ports.testing_calls == []
    assert ports.result_calls == []
    assert ports.reports == []
