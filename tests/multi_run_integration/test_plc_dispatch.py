from __future__ import annotations

import threading
import time
from collections.abc import Mapping

import pytest

from app.service.plc_result_sequencer import (
    PlcMutationDisposition,
    PlcOperationKind,
    PlcResultBoundaryError,
    PlcResultSequencer,
)


def _payload(label: str, *, error_code: int = 0, passed: bool = True) -> dict[str, object]:
    return {
        "finished": True,
        "error_code": error_code,
        "sn_code": f"SN-{label}",
        "test_result": passed,
        "score": 91.5,
        "spl": 62.25,
        "loudness": 4.5,
        "vibrate": 0.125,
        "ai_score": 88.75,
    }


class _RunningThread:
    def __init__(self, running: bool = True) -> None:
        self.running = running

    def isRunning(self) -> bool:
        return self.running


class _CommandSignal:
    def __init__(self, service: "_ServiceProbe") -> None:
        self._service = service
        self.emit_count = 0

    def emit(self, command: object) -> None:
        self.emit_count += 1
        command.execute(self._service)


class _ServiceProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, object]] = []
        self.plc_dispatch_requested = _CommandSignal(self)
        self.result_error: Exception | None = None
        self.testing_error: Exception | None = None

    def write_result(self, plc_id: int, payload: Mapping[str, object]) -> None:
        self.calls.append(("result", plc_id, dict(payload)))
        if self.result_error is not None:
            raise self.result_error

    def write_testing_state(self, plc_id: int, testing: bool) -> None:
        self.calls.append(("testing", plc_id, testing))
        if self.testing_error is not None:
            raise self.testing_error


def _ports(plc_ids: tuple[int, ...] = (1, 2), *, timeout: float = 0.1):
    from app.service.multi_run_integration.plc_dispatch import PlcDispatchPorts

    services = {plc_id: _ServiceProbe() for plc_id in plc_ids}
    threads = {plc_id: _RunningThread() for plc_id in plc_ids}
    return (
        PlcDispatchPorts(
            plc_services=services,
            plc_threads=threads,
            dispatch_timeout_seconds=timeout,
        ),
        services,
        threads,
    )


def _register(sequencer: PlcResultSequencer, run_id: str, plc_id: int):
    registration = sequencer.register(
        run_id=run_id,
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=plc_id,
    )
    assert registration.disposition is PlcMutationDisposition.APPLIED
    assert registration.ticket is not None
    return registration.ticket


def test_result_and_testing_ports_dispatch_once_in_per_plc_fifo_order():
    ports, services, _threads = _ports()
    reports = []
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=reports.append,
    )
    first = _register(sequencer, "plc-1-first", 1)
    second = _register(sequencer, "plc-1-second", 1)
    independent = _register(sequencer, "plc-2-first", 2)

    assert sequencer.mark_accepted(first) is PlcMutationDisposition.APPLIED
    assert sequencer.mark_accepted(second) is PlcMutationDisposition.APPLIED
    assert sequencer.mark_accepted(independent) is PlcMutationDisposition.APPLIED
    assert (
        sequencer.publish_terminal(second, _payload("SECOND"))
        is PlcMutationDisposition.APPLIED
    )
    assert [call for call in services[1].calls if call[0] == "result"] == []

    assert (
        sequencer.publish_terminal(independent, _payload("INDEPENDENT"))
        is PlcMutationDisposition.APPLIED
    )
    assert (
        sequencer.publish_terminal(first, _payload("FIRST"))
        is PlcMutationDisposition.APPLIED
    )

    assert [
        call[2]["sn_code"]
        for call in services[1].calls
        if call[0] == "result"
    ] == ["SN-FIRST", "SN-SECOND"]
    assert [
        call[2]["sn_code"]
        for call in services[2].calls
        if call[0] == "result"
    ] == ["SN-INDEPENDENT"]
    assert [call for call in services[1].calls if call[0] == "testing"] == [
        ("testing", 1, True),
        ("testing", 1, False),
    ]
    assert [call for call in services[2].calls if call[0] == "testing"] == [
        ("testing", 2, True),
        ("testing", 2, False),
    ]
    assert services[1].plc_dispatch_requested.emit_count == 4
    assert services[2].plc_dispatch_requested.emit_count == 3
    assert len(reports) == 7


def test_pre_ticket_invalid_duplicate_or_closed_has_no_card_or_settlement():
    ports, services, _threads = _ports((1,))
    reports = []
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=reports.append,
    )

    with pytest.raises(ValueError):
        sequencer.register(
            run_id="invalid",
            trigger_source="plc",
            operation_kind=PlcOperationKind.INITIAL,
            plc_id=True,
        )
    ticket = _register(sequencer, "ticketed", 1)
    duplicate = sequencer.register(
        run_id="ticketed",
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=1,
    )
    assert duplicate.ticket is None
    assert duplicate.disposition is PlcMutationDisposition.INVALID_STATE
    assert services[1].calls == []
    assert reports == []

    rejected_payload = _payload("PREACCEPT", error_code=11, passed=False)
    assert (
        sequencer.publish_rejection(ticket, rejected_payload)
        is PlcMutationDisposition.APPLIED
    )
    assert [call[2] for call in services[1].calls if call[0] == "result"] == [
        rejected_payload
    ]
    assert len(reports) == 1

    sequencer.shutdown(0.1)
    calls_after_shutdown = list(services[1].calls)
    reports_after_shutdown = list(reports)
    closed = sequencer.register(
        run_id="closed",
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=1,
    )
    assert closed.ticket is None
    assert closed.disposition is PlcMutationDisposition.CLOSED
    assert services[1].calls == calls_after_shutdown
    assert reports == reports_after_shutdown


def test_post_active_unaccepted_and_sync_start_race_preserve_ticket_order():
    ports, services, _threads = _ports((1,))
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=lambda _report: None,
    )
    synchronous = _register(sequencer, "sync-completion", 1)
    capture_start_failed = _register(sequencer, "post-active-unaccepted", 1)
    held_terminal = _payload("SYNC")

    # Coordinator holds a synchronous Resident completion until start returns
    # and the acquired ticket has been accepted.
    assert [call for call in services[1].calls if call[0] == "result"] == []
    assert sequencer.mark_accepted(synchronous) is PlcMutationDisposition.APPLIED
    assert (
        sequencer.publish_terminal(synchronous, held_terminal)
        is PlcMutationDisposition.APPLIED
    )
    assert (
        sequencer.publish_terminal(
            capture_start_failed,
            _payload("START-FAILED", error_code=12, passed=False),
        )
        is PlcMutationDisposition.APPLIED
    )

    assert [
        call[2]["sn_code"]
        for call in services[1].calls
        if call[0] == "result"
    ] == ["SN-SYNC", "SN-START-FAILED"]
    assert [call for call in services[1].calls if call[0] == "testing"] == [
        ("testing", 1, True),
        ("testing", 1, False),
    ]


def test_every_allowed_terminal_payload_dispatches_once():
    ports, services, _threads = _ports((1,))
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=lambda _report: None,
    )
    terminal_classes = (
        ("OK", 0, True),
        ("NG", 1, False),
        ("NO-RESULT", 2, False),
        ("ANALYSIS-ERROR", 3, False),
        ("SYSTEM-ERROR", 4, False),
        ("MANUAL-STOP", 5, False),
        ("SHUTDOWN-CANCEL", 6, False),
        ("TERMINAL-PERSISTENCE-FAILURE", 7, False),
    )

    for label, error_code, passed in terminal_classes:
        ticket = _register(sequencer, label, 1)
        assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
        assert (
            sequencer.publish_terminal(
                ticket,
                _payload(label, error_code=error_code, passed=passed),
            )
            is PlcMutationDisposition.APPLIED
        )

    results = [call[2] for call in services[1].calls if call[0] == "result"]
    assert [payload["sn_code"] for payload in results] == [
        f"SN-{label}" for label, _error_code, _passed in terminal_classes
    ]
    assert len(results) == len(terminal_classes)


def test_non_plc_manual_and_reanalysis_perform_zero_plc_writes():
    ports, services, _threads = _ports((1,))
    reports = []
    sequencer = PlcResultSequencer(
        write_result=ports.write_result,
        write_testing_state=ports.write_testing_state,
        report_attempt=reports.append,
    )

    registrations = (
        sequencer.register(
            run_id="scanner",
            trigger_source="scanner",
            operation_kind=PlcOperationKind.INITIAL,
            plc_id=None,
        ),
        sequencer.register(
            run_id="manual",
            trigger_source="manual",
            operation_kind=PlcOperationKind.MANUAL_JUDGMENT,
            plc_id=None,
        ),
        sequencer.register(
            run_id="reanalysis",
            trigger_source="manual",
            operation_kind=PlcOperationKind.REANALYSIS,
            plc_id=None,
        ),
    )

    assert all(
        result.disposition is PlcMutationDisposition.EXCLUDED
        and result.ticket is None
        for result in registrations
    )
    assert services[1].calls == []
    assert reports == []


def test_dispatch_surfaces_validation_target_and_one_attempt_failures():
    ports, services, threads = _ports()

    with pytest.raises(PlcResultBoundaryError) as invalid_id:
        ports.write_result(True, _payload("INVALID-ID"))
    assert invalid_id.value.field_name == "plc_id"

    with pytest.raises(PlcResultBoundaryError) as semantic_payload:
        ports.write_result(
            1,
            {
                "run_id": "run",
                "source": "plc",
                "conclusion": "OK",
            },
        )
    assert semantic_payload.value.field_name == "payload"

    with pytest.raises(PlcResultBoundaryError) as invalid_testing:
        ports.write_testing_state(1, 1)
    assert invalid_testing.value.field_name == "testing"

    threads[1].running = False
    with pytest.raises(PlcResultBoundaryError) as stopped:
        ports.write_result(1, _payload("STOPPED"))
    assert stopped.value.field_name == "result_dispatch"
    assert services[1].plc_dispatch_requested.emit_count == 0

    threads[1].running = True
    services[1].result_error = PlcResultBoundaryError(
        "res_score_offset",
        "score write failed",
    )
    with pytest.raises(PlcResultBoundaryError) as field_failure:
        ports.write_result(1, _payload("FAILED"))
    assert field_failure.value.field_name == "res_score_offset"
    assert services[1].plc_dispatch_requested.emit_count == 1

    services[1].result_error = None
    ports.write_result(1, _payload("NEXT-ONE-SHOT"))
    assert services[1].plc_dispatch_requested.emit_count == 2
    assert services[1].calls[-1][2]["sn_code"] == "SN-NEXT-ONE-SHOT"

    ports.write_result(2, _payload("OTHER-PLC"))
    assert services[2].plc_dispatch_requested.emit_count == 1
    assert [call[2]["sn_code"] for call in services[2].calls] == [
        "SN-OTHER-PLC"
    ]


def test_pending_dispatch_timeout_cancels_retained_command_without_late_mutation():
    ports, services, _threads = _ports((1,), timeout=0.01)

    class _NonCompletingSignal:
        def __init__(self) -> None:
            self.emit_count = 0
            self.command = None

        def emit(self, command: object) -> None:
            self.emit_count += 1
            self.command = command

    signal = _NonCompletingSignal()
    services[1].plc_dispatch_requested = signal

    started_at = time.monotonic()
    with pytest.raises(PlcResultBoundaryError) as failure:
        ports.write_testing_state(1, True)
    elapsed = time.monotonic() - started_at

    assert failure.value.field_name == "testing_dispatch"
    assert elapsed < 0.2
    assert signal.emit_count == 1
    assert signal.command is not None
    signal.command.execute(services[1])
    assert services[1].calls == []
    assert not any("pending" in name for name in vars(ports))


def test_started_qt_dispatch_waits_for_actual_boundary_failure(qapp):
    from PyQt6.QtCore import QThread

    from app.service.multi_run_integration.plc_dispatch import PlcDispatchPorts
    from app.service.plc_service import PlcService

    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    failures = []

    class _SlowFailingService(PlcService):
        def __init__(self) -> None:
            super().__init__(
                bus=object(),
                on_start_requested=lambda *_args: None,
                ip="127.0.0.1",
                plc_id=1,
            )
            self.result_calls = 0

        def write_result(self, _plc_id, _payload) -> None:
            self.result_calls += 1
            entered.set()
            assert release.wait(1)
            raise PlcResultBoundaryError(
                "res_score_offset",
                "actual slow field failure",
            )

    service = _SlowFailingService()
    qt_thread = QThread()
    service.moveToThread(qt_thread)
    qt_thread.start()
    ports = PlcDispatchPorts(
        plc_services={1: service},
        plc_threads={1: qt_thread},
        dispatch_timeout_seconds=0.01,
    )

    def call_port() -> None:
        try:
            ports.write_result(1, _payload("SLOW"))
        except Exception as exc:
            failures.append(exc)
        finally:
            returned.set()

    caller = threading.Thread(target=call_port)
    caller.start()
    try:
        assert entered.wait(1)
        assert not returned.wait(0.05)
        release.set()
        assert returned.wait(1)
        caller.join(1)
        assert not caller.is_alive()
    finally:
        release.set()
        caller.join(1)
        qt_thread.quit()
        assert qt_thread.wait(1_000)

    assert service.result_calls == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PlcResultBoundaryError)
    assert failures[0].field_name == "res_score_offset"
    assert failures[0].reason == "actual slow field failure"


def test_dispatch_runs_on_the_owned_plc_service_qt_thread(qapp):
    from PyQt6.QtCore import QThread

    from app.service.multi_run_integration.plc_dispatch import PlcDispatchPorts
    from app.service.plc_service import PlcService

    class _ThreadRecordingService(PlcService):
        def __init__(self) -> None:
            super().__init__(
                bus=object(),
                on_start_requested=lambda *_args: None,
                ip="127.0.0.1",
                plc_id=1,
            )
            self.calls = []

        def write_result(self, plc_id, payload) -> None:
            self.calls.append(
                (
                    "result",
                    self.thread() is QThread.currentThread(),
                    plc_id,
                    dict(payload),
                )
            )

        def write_testing_state(self, plc_id, testing) -> None:
            self.calls.append(
                (
                    "testing",
                    self.thread() is QThread.currentThread(),
                    plc_id,
                    testing,
                )
            )

    service = _ThreadRecordingService()
    thread = QThread()
    service.moveToThread(thread)
    thread.start()
    ports = PlcDispatchPorts(
        plc_services={1: service},
        plc_threads={1: thread},
    )
    try:
        ports.write_result(1, _payload("THREAD"))
        ports.write_testing_state(1, True)
    finally:
        thread.quit()
        assert thread.wait(1_000)

    assert [call[:3] for call in service.calls] == [
        ("result", True, 1),
        ("testing", True, 1),
    ]
