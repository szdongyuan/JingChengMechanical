from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread
import time
from types import SimpleNamespace
from dataclasses import replace

import pytest

import app.service.multi_run_coordinator_mvp as package
from app.service.multi_run_coordinator_mvp import (
    AudioSaveOutcome,
    AudioSaveSourceKind,
    AudioSaveStatus,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CaptureReserveResult,
    CaptureReserveStatus,
    CommitResult,
    CoordinatorLifecycle,
    CoordinatorPhase,
    ExternalActionKind,
    HistoryFactKind,
    PlcMutationDisposition,
    SaveSubmitStatus,
    StartStatus,
    PreacceptTerminalFact,
    TerminalFact,
    TerminalConclusion,
    TerminalErrorCategory,
    TriggerSource,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    AnalysisJob,
    AnalysisOutcome,
    AnalysisOutcomeStatus,
    SchedulerSubmitStatus,
    CaptureReservePending,
    _issue_capture_reserve_claim,
    _issue_capture_reserve_wake_signal,
)
from app.service.captured_start_dispatcher import (
    CapturedStartDispatcher,
)

from ._support import (
    RUN_IDS,
    ATTEMPT_IDS,
    SAVE_JOB_IDS,
    AUDIO_IDS,
    Harness,
    FakeResident,
    FakeCurrentCollector,
    captured_request,
    completed_capture,
    frozen_captured,
    imported_request,
    saved_outcome,
    completed_analysis,
    payload,
)


class _PendingResident(FakeResident):
    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.owner_nonce = object()
        self.ready = Event()
        self.begin_calls: list[tuple[str, str, int, int]] = []
        self.resume_calls: list[object] = []
        self.cancel_reserve_calls: list[object] = []
        self.claim = None
        self.pending_hardware_ids = {"hardware-1"}

    def begin_reserve(
        self,
        run_id: str,
        hardware_id: str,
        trigger_monotonic_ns: int,
        deadline_monotonic_ns: int,
    ) -> CaptureReservePending:
        self.trace.append("begin_reserve")
        self.begin_calls.append(
            (
                run_id,
                hardware_id,
                trigger_monotonic_ns,
                deadline_monotonic_ns,
            )
        )
        if hardware_id not in self.pending_hardware_ids:
            return self.reserve(run_id, hardware_id)
        self.claim = _issue_capture_reserve_claim(
            claim_id=f"claim-{run_id}",
            run_id=run_id,
            hardware_id=hardware_id,
            trigger_monotonic_ns=trigger_monotonic_ns,
            deadline_monotonic_ns=deadline_monotonic_ns,
            owner_nonce=self.owner_nonce,
        )
        return CaptureReservePending(
            run_id,
            hardware_id,
            self.claim,
            _issue_capture_reserve_wake_signal(
                claim=self.claim,
                owner_nonce=self.owner_nonce,
                wait=lambda timeout: (
                    True if self.ready.wait(timeout) else None
                ),
            ),
        )

    def resume_reserve(self, claim):
        self.trace.append("resume_reserve")
        self.resume_calls.append(claim)
        return self.reserve(claim.run_id, claim.hardware_id)

    def cancel_reserve(self, claim) -> bool:
        self.trace.append("cancel_reserve")
        self.cancel_reserve_calls.append(claim)
        self.ready.set()
        return True


def _assert_error_physical_plc_payload(value: object) -> None:
    assert type(value) is dict
    assert set(value) == {
        "finished",
        "error_code",
        "sn_code",
        "test_result",
        "score",
        "spl",
        "loudness",
        "vibrate",
        "ai_score",
    }
    assert value["finished"] is True
    assert type(value["error_code"]) is int
    assert value["error_code"] != 0
    assert value["sn_code"] == ""
    assert value["test_result"] is False
    assert {
        value["score"],
        value["spl"],
        value["loudness"],
        value["vibrate"],
        value["ai_score"],
    } == {0.0}


def test_admission_package_root_exports_coordinator() -> None:
    assert package.MultiRunCoordinator.__name__ == "MultiRunCoordinator"


def test_resident_pending_parks_then_resumes_same_claim_and_original_trigger() -> None:
    harness = Harness()
    resident = _PendingResident(harness.trace)
    harness.resident = resident
    coordinator = harness.coordinator()
    dispatcher = CapturedStartDispatcher(capacity=2, worker_count=2)
    request = captured_request()

    def start():
        return coordinator.start_captured(request)

    try:
        assert dispatcher.try_submit(start, lane_key="hardware-1")
        deadline = time.monotonic() + 2.0
        while not resident.begin_calls:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert len(harness.captured_freezer.calls) == 1
        assert len(resident.begin_calls) == 1
        assert resident.resume_calls == []
        assert harness.persistence.active_calls == []
        with coordinator._lock:
            context = coordinator._runs[request.run_id]
            assert context.action is None
            assert context.reserve_claim is resident.claim

        resident.ready.set()
        deadline = time.monotonic() + 2.0
        while not resident.start_calls:
            assert time.monotonic() < deadline
            time.sleep(0.005)

        assert resident.resume_calls == [resident.claim]
        assert len(harness.captured_freezer.calls) == 1
        assert len(resident.begin_calls) == 1
        assert resident.begin_calls[0][2] == request.trigger_monotonic_ns
        assert resident.start_calls[0][1] == request.trigger_monotonic_ns
    finally:
        resident.ready.set()
        assert dispatcher.close(timeout_seconds=2.0)


def test_parked_hardware_does_not_block_healthy_hardware_start() -> None:
    harness = Harness()
    resident = _PendingResident(harness.trace)
    harness.resident = resident
    harness.captured_freezer.result_factory = lambda request: replace(
        frozen_captured(request),
        hardware_id=(
            "hardware-1" if request.run_id == RUN_IDS[0] else "hardware-2"
        ),
    )
    coordinator = harness.coordinator()
    dispatcher = CapturedStartDispatcher(capacity=2, worker_count=2)

    try:
        assert dispatcher.try_submit(
            lambda: coordinator.start_captured(captured_request(RUN_IDS[0])),
            lane_key="hardware-1",
        )
        deadline = time.monotonic() + 2.0
        while not resident.begin_calls:
            assert time.monotonic() < deadline
            time.sleep(0.005)

        assert dispatcher.try_submit(
            lambda: coordinator.start_captured(captured_request(RUN_IDS[1])),
            lane_key="hardware-2",
        )
        assert not dispatcher.try_submit(
            lambda: None,
            lane_key="hardware-3",
        )
        while not any(
            reservation.hardware_id == "hardware-2"
            for reservation, _, _ in resident.start_calls
        ):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert resident.resume_calls == []
    finally:
        resident.ready.set()
        assert dispatcher.close(timeout_seconds=2.0)


def test_admission_invalid_request_calls_no_port() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_captured(SimpleNamespace(run_id=RUN_IDS[0]))

    assert result.status is StartStatus.INVALID_REQUEST
    assert harness.trace == []


def test_admission_duplicate_run_id_calls_no_new_port() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    request = captured_request()
    assert coordinator.start_captured(request).status is StartStatus.ACCEPTED_ACTIVE
    trace = list(harness.trace)

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.DUPLICATE_RUN_ID
    assert harness.trace == trace


def test_admission_six_same_config_runs_remain_separate() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    results = [
        coordinator.start_captured(captured_request(run_id))
        for run_id in RUN_IDS[:6]
    ]

    assert [result.status for result in results] == [
        StartStatus.ACCEPTED_ACTIVE
    ] * 6
    assert coordinator.snapshot().accepted_active_run_ids == tuple(
        sorted(RUN_IDS[:6])
    )
    assert len(harness.persistence.active_calls) == 6
    assert {
        frozen.config_id for frozen in harness.persistence.active_calls
    } == {"same-config"}


def test_each_accepted_run_owns_one_isolated_current_collector() -> None:
    harness = Harness()
    collectors = [
        FakeCurrentCollector(harness.trace)
        for _ in range(6)
    ]
    harness.current_factory.collectors.extend(collectors)
    harness.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
    )
    coordinator = harness.coordinator()

    results = [
        coordinator.start_captured(captured_request(run_id))
        for run_id in RUN_IDS[:6]
    ]

    assert [result.status for result in results] == [
        StartStatus.ACCEPTED_ACTIVE
    ] * 6
    assert [run_id for run_id, _ in harness.current_factory.calls] == list(
        RUN_IDS[:6]
    )
    assert [
        frozen for _, frozen in harness.current_factory.calls
    ] == harness.persistence.active_calls
    assert [collector.start_calls for collector in collectors] == [1] * 6
    assert len({id(collector) for collector in collectors}) == 6
    assert harness.resident.start_calls and len(harness.resident.start_calls) == 6

    disabled = Harness()
    disabled_coordinator = disabled.coordinator()
    assert disabled_coordinator.start_captured(captured_request()).accepted
    assert len(disabled.current_factory.calls) == 1
    assert "current_start" not in disabled.trace

    imported = Harness()
    imported_coordinator = imported.coordinator()
    assert imported_coordinator.start_imported(imported_request()).accepted
    assert imported.current_factory.calls == []


def test_enabled_current_create_or_start_failure_is_pre_resident_current_invalid() -> None:
    cases = ("factory-none", "factory-exception", "start-false", "start-exception")
    for index, case in enumerate(cases):
        harness = Harness()
        harness.captured_freezer.result_factory = lambda request: frozen_captured(
            request,
            current_enabled=True,
        )
        collector = FakeCurrentCollector(harness.trace)
        if case == "factory-none":
            harness.current_factory.collectors.append(None)
        elif case == "factory-exception":
            harness.current_factory.exception = RuntimeError("create failed")
        elif case == "start-false":
            collector.start_result = False
            harness.current_factory.collectors.append(collector)
        else:
            collector.start_exception = RuntimeError("start failed")
            harness.current_factory.collectors.append(collector)
        coordinator = harness.coordinator()

        result = coordinator.start_captured(captured_request(RUN_IDS[index]))

        assert result.status is StartStatus.ACCEPTED_TERMINAL
        assert result.terminal is not None
        assert (
            result.terminal.error_category
            is TerminalErrorCategory.CURRENT_TRACE_INVALID
        )
        assert (
            "create" if case.startswith("factory") else "start"
        ) in result.terminal.reason
        assert harness.resident.register_calls == []
        assert harness.resident.start_calls == []
        assert harness.persistence.current_calls == []
        assert harness.save_dispatcher.calls == []
        assert harness.scheduler.initial_calls == []


def test_admission_seventh_capacity_is_resident_authority() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    for run_id in RUN_IDS[:6]:
        assert (
            coordinator.start_captured(captured_request(run_id)).status
            is StartStatus.ACCEPTED_ACTIVE
        )
    harness.resident.reserve_results.append(
        CaptureReserveResult(
            status=CaptureReserveStatus.CAPACITY,
            run_id=RUN_IDS[6],
            hardware_id="hardware-1",
            reservation=None,
            reason="resident capacity",
        )
    )

    result = coordinator.start_captured(captured_request(RUN_IDS[6]))

    assert result.status is StartStatus.CAPTURE_CAPACITY
    assert len(harness.resident.reserve_calls) == 7
    assert len(harness.persistence.active_calls) == 6


def test_admission_non_plc_never_calls_plc() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_captured(
        captured_request(source=TriggerSource.SCANNER)
    )

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert not [
        call for call in harness.trace if call.startswith("plc_")
    ]


def test_ticket_is_acquired_before_freezer() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert harness.trace.index("plc_register") < harness.trace.index("freeze")
    assert harness.plc.register_calls == [
        {
            "run_id": RUN_IDS[0],
            "trigger_source": "plc",
            "operation_kind": package.PlcOperationKind.INITIAL,
            "plc_id": 1,
        }
    ]


@pytest.mark.parametrize(
    "registration_outcome",
    [
        PlcMutationDisposition.EXCLUDED,
        PlcMutationDisposition.DUPLICATE_OR_STALE,
        PlcMutationDisposition.INVALID_STATE,
        PlcMutationDisposition.CLOSED,
        object(),
        "applied-without-ticket",
        RuntimeError("register failed"),
    ],
)
def test_ticket_registration_failure_has_no_card_or_settlement(
    registration_outcome,
) -> None:
    harness = Harness()
    if isinstance(registration_outcome, PlcMutationDisposition):
        harness.plc.registration_disposition = registration_outcome
    elif registration_outcome == "applied-without-ticket":
        harness.plc.registration_result = package.PlcRegistrationResult(
            PlcMutationDisposition.APPLIED,
            None,
        )
    elif isinstance(registration_outcome, Exception):
        harness.plc.register_exception = registration_outcome
    else:
        harness.plc.registration_result = registration_outcome
    coordinator = harness.coordinator()

    result = coordinator.start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.status is StartStatus.PLC_INGRESS_REJECTED
    assert harness.plc.reject_calls == []
    assert harness.plc.publish_calls == []
    assert harness.captured_freezer.calls == []
    assert harness.projector.calls == []
    assert harness.history.calls == []
    assert coordinator.snapshot().ingress_run_ids == ()
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "PLC registration"
    ]
    assert len(diagnostics) == 1
    assert len(diagnostics[0].message.encode("utf-8")) <= 512


@pytest.mark.parametrize(
    "stage",
    [
        "configuration",
        "hardware",
        "capacity",
        "reserve-exception",
        "active-rejected",
        "active-exception",
    ],
)
def test_ticketed_pre_active_failure_publishes_one_rejection(stage: str) -> None:
    harness = Harness()
    if stage == "configuration":
        harness.captured_freezer.exception = RuntimeError("configuration failed")
    elif stage == "hardware":
        harness.captured_freezer.result_factory = lambda request: replace(
            frozen_captured(request),
            run_id=RUN_IDS[1],
        )
    elif stage == "capacity":
        harness.resident.reserve_results.append(
            CaptureReserveResult(
                CaptureReserveStatus.CAPACITY,
                RUN_IDS[0],
                "hardware-1",
                None,
                "capacity",
            )
        )
    elif stage == "reserve-exception":
        harness.resident.reserve_exception = RuntimeError("reserve failed")
    elif stage == "active-rejected":
        harness.persistence.active_results.append(CommitResult.REJECTED)
    else:
        harness.persistence.active_exception = RuntimeError("active failed")

    result = harness.coordinator().start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.accepted is False
    assert len(harness.plc.reject_calls) == 1
    ticket, payload = harness.plc.reject_calls[0]
    assert type(ticket) is package.PlcIngressTicket
    assert ticket == package.PlcIngressTicket(RUN_IDS[0], 1, 1)
    _assert_error_physical_plc_payload(payload)
    assert harness.plc.publish_calls == []
    assert harness.plc.mark_calls == []


def test_admission_run_id_stays_duplicate_while_publication_is_blocked() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    harness.resident.reserve_results.append(
        CaptureReserveResult(
            status=CaptureReserveStatus.CAPACITY,
            run_id=RUN_IDS[0],
            hardware_id="hardware-1",
            reservation=None,
            reason="resident capacity",
        )
    )
    entered = Event()
    release = Event()
    harness.history.hook.entered = entered
    harness.history.hook.release = release
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )
    thread.start()
    assert entered.wait(2)

    duplicate = coordinator.start_captured(captured_request())

    assert duplicate.status is StartStatus.DUPLICATE_RUN_ID
    assert coordinator.snapshot().pending_publication_run_ids == (RUN_IDS[0],)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert result_box[0].status is StartStatus.CAPTURE_CAPACITY
    assert harness.history.calls[0].kind is HistoryFactKind.RUN_TERMINATED
    assert coordinator.snapshot().pending_publication_run_ids == ()


def test_admission_accepted_phase_is_recording() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert result.phase is CoordinatorPhase.RECORDING


def test_captured_acceptance_order_is_exact() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert harness.trace == [
        "plc_register",
        "freeze",
            "reserve",
            "create_active",
            "current_factory",
            "register",
        "project:RECORDING",
        "history:RECORDING",
        "start",
        "plc_mark",
    ]


@pytest.mark.parametrize("raises", [False, True])
def test_captured_freezer_mismatch_or_exception_rejects_ticket(
    raises: bool,
) -> None:
    harness = Harness()
    request = captured_request(source=TriggerSource.PLC)
    if raises:
        harness.captured_freezer.exception = RuntimeError("freeze unavailable")
        expected = StartStatus.PREFLIGHT_REJECTED
    else:
        harness.captured_freezer.result_factory = lambda request: replace(
            frozen_captured(request), run_id=RUN_IDS[1]
        )
        expected = StartStatus.INVALID_FROZEN_INPUT
    coordinator = harness.coordinator()

    result = coordinator.start_captured(request)

    assert result.status is expected
    assert len(harness.plc.reject_calls) == 1
    assert harness.persistence.active_calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CaptureReserveStatus.CAPACITY, StartStatus.CAPTURE_CAPACITY),
        (CaptureReserveStatus.NOT_READY, StartStatus.RESIDENT_UNAVAILABLE),
        (CaptureReserveStatus.FAULT, StartStatus.RESIDENT_UNAVAILABLE),
        (CaptureReserveStatus.CLOSING, StartStatus.RESIDENT_UNAVAILABLE),
    ],
)
def test_captured_resident_rejection_never_creates_active(
    status: CaptureReserveStatus, expected: StartStatus
) -> None:
    harness = Harness()
    harness.resident.reserve_results.append(
        CaptureReserveResult(
            status=status,
            run_id=RUN_IDS[0],
            hardware_id="hardware-1",
            reservation=None,
            reason=status.name,
        )
    )
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is expected
    assert harness.persistence.active_calls == []


@pytest.mark.parametrize("raises", [False, True])
def test_captured_active_commit_failure_cancels_reservation_once(
    raises: bool,
) -> None:
    harness = Harness()
    if raises:
        harness.persistence.active_exception = RuntimeError("db unavailable")
    else:
        harness.persistence.active_results.append(CommitResult.REJECTED)
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACTIVE_COMMIT_REJECTED
    assert len(harness.resident.cancel_calls) == 1
    assert harness.persistence.terminal_calls == []


@pytest.mark.parametrize("raises", [False, True])
def test_captured_plc_mark_failure_terminalizes_accepted_run(
    raises: bool,
) -> None:
    harness = Harness()
    if raises:
        harness.plc.mark_exception = RuntimeError("PLC unavailable")
    else:
        harness.plc.mark_result = PlcMutationDisposition.INVALID_STATE
    coordinator = harness.coordinator()

    result = coordinator.start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is TerminalErrorCategory.PLC_ACTIVATION_FAILED
    assert len(harness.persistence.terminal_calls) == 1
    assert len(harness.plc.publish_calls) == 1
    assert harness.plc.reject_calls == []


@pytest.mark.parametrize("raises", [False, True])
def test_captured_plc_mark_failure_cancels_binding_before_terminal_commit(
    raises: bool,
) -> None:
    harness = Harness()
    if raises:
        harness.plc.mark_exception = RuntimeError("PLC unavailable")
    else:
        harness.plc.mark_result = PlcMutationDisposition.INVALID_STATE
    entered = Event()
    release = Event()
    harness.resident.hooks["cancel"].entered = entered
    harness.resident.hooks["cancel"].release = release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(
                captured_request(source=TriggerSource.PLC)
            )
        )
    )

    thread.start()
    assert entered.wait(2)
    context = coordinator._runs[RUN_IDS[0]]
    assert context.action.kind is ExternalActionKind.CHILD_CANCEL
    assert harness.persistence.terminal_calls == []

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.resident.cancel_calls) == 1
    assert harness.trace.index("plc_mark") < harness.trace.index(
        "resident_cancel"
    )
    assert harness.trace.index("resident_cancel") < harness.trace.index(
        "commit_terminal"
    )


def test_captured_terminal_contender_during_post_start_plc_mark_settles_once() -> None:
    harness = Harness()
    entered = Event()
    release = Event()
    harness.plc.hooks["mark"].entered = entered
    harness.plc.hooks["mark"].release = release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(
                captured_request(source=TriggerSource.PLC)
            )
        )
    )

    thread.start()
    assert entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    pending_result = coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.CAPTURE_FAILED,
        "terminal contender during PLC mark",
    )

    assert pending_result.status is StartStatus.ACCEPTED_ACTIVE
    assert pending_result.phase is CoordinatorPhase.STARTING
    assert harness.persistence.terminal_calls == []

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.persistence.terminal_calls) == 1
    assert (
        harness.persistence.terminal_calls[0].error_category
        is TerminalErrorCategory.CAPTURE_FAILED
    )
    assert len(harness.resident.register_calls) == 1
    assert [
        event
        for event in harness.history.calls
        if event.kind is HistoryFactKind.RECORDING
    ]
    assert len(harness.plc.publish_calls) == 1
    assert harness.plc.reject_calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        PlcMutationDisposition.EXCLUDED,
        PlcMutationDisposition.DUPLICATE_OR_STALE,
        PlcMutationDisposition.INVALID_STATE,
        PlcMutationDisposition.CLOSED,
        object(),
        RuntimeError("mark failed"),
    ],
)
def test_plc_mark_non_applied_or_exception_fails_closed_once(outcome) -> None:
    harness = Harness()
    if isinstance(outcome, Exception):
        harness.plc.mark_exception = outcome
    else:
        harness.plc.mark_result = outcome

    result = harness.coordinator().start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.plc.mark_calls) == 1
    assert len(harness.plc.publish_calls) == 1
    assert harness.plc.reject_calls == []
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "PLC mark accepted"
    ]
    assert len(diagnostics) == 1
    assert len(diagnostics[0].message.encode("utf-8")) <= 512


def test_captured_registration_failure_cancels_and_terminalizes() -> None:
    harness = Harness()
    harness.resident.register_exception = RuntimeError("registration failed")
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert (
        result.terminal.error_category
        is TerminalErrorCategory.CAPTURE_REGISTRATION_FAILED
    )
    assert len(harness.resident.cancel_calls) == 1
    assert len(harness.persistence.terminal_calls) == 1


def test_captured_start_failure_cancels_and_terminalizes() -> None:
    harness = Harness()
    harness.resident.start_exception = RuntimeError("start failed")
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is TerminalErrorCategory.CAPTURE_START_FAILED
    assert len(harness.resident.cancel_calls) == 1
    assert len(harness.persistence.terminal_calls) == 1


@pytest.mark.parametrize("stage", ["registration", "start", "plc"])
def test_started_current_is_settled_once_on_capture_activation_failure(
    stage: str,
) -> None:
    harness = Harness()
    collector = FakeCurrentCollector(harness.trace)
    harness.current_factory.collectors.append(collector)
    harness.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
    )
    source = TriggerSource.PLC if stage == "plc" else TriggerSource.MANUAL
    if stage == "registration":
        harness.resident.register_exception = RuntimeError(
            "registration failed"
        )
        category = TerminalErrorCategory.CAPTURE_REGISTRATION_FAILED
    elif stage == "start":
        harness.resident.start_exception = RuntimeError("start failed")
        category = TerminalErrorCategory.CAPTURE_START_FAILED
    else:
        harness.plc.mark_exception = RuntimeError("PLC unavailable")
        category = TerminalErrorCategory.PLC_ACTIVATION_FAILED

    result = harness.coordinator().start_captured(
        captured_request(source=source)
    )

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is category
    assert collector.start_calls == 1
    assert len(collector.stop_calls) == 1
    assert collector.samples_calls == 1
    assert harness.persistence.current_calls == []
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.initial_calls == []


def test_active_shutdown_settles_started_current_before_clean_report() -> None:
    harness = Harness()
    collector = FakeCurrentCollector(harness.trace)
    harness.current_factory.collectors.append(collector)
    harness.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
    )
    coordinator = harness.coordinator()
    assert coordinator.start_captured(captured_request()).accepted

    coordinator.begin_shutdown()
    report = coordinator.finish_shutdown(1.0)

    assert report.status is package.CoordinatorShutdownStatus.CLOSED
    assert collector.start_calls == 1
    assert len(collector.stop_calls) == 1
    assert collector.samples_calls == 1
    assert len(harness.persistence.terminal_calls) == 1
    terminal = harness.persistence.terminal_calls[0]
    assert terminal.error_category is TerminalErrorCategory.MANUAL_STOPPED
    assert terminal.reason == "Coordinator lifecycle stop requested"


def test_shutdown_racing_current_start_settles_collector_that_started() -> None:
    harness = Harness()
    collector = FakeCurrentCollector(harness.trace)
    current_start_entered = Event()
    current_start_release = Event()
    collector.hooks["start"].entered = current_start_entered
    collector.hooks["start"].release = current_start_release
    harness.current_factory.collectors.append(collector)
    harness.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
    )
    coordinator = harness.coordinator()
    result_box = []
    starter = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )

    starter.start()
    assert current_start_entered.wait(2)
    coordinator.begin_shutdown()
    current_start_release.set()
    starter.join(2)

    assert not starter.is_alive()
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    assert collector.start_calls == 1
    assert len(collector.stop_calls) == 1
    assert collector.samples_calls == 1
    report = coordinator.finish_shutdown(1.0)
    assert report.status is package.CoordinatorShutdownStatus.CLOSED


@pytest.mark.parametrize("stage", ["registration", "start"])
def test_plc_post_active_capture_failure_publishes_unaccepted_terminal_once(
    stage: str,
) -> None:
    harness = Harness()
    if stage == "registration":
        harness.resident.register_exception = RuntimeError(
            "registration failed"
        )
    else:
        harness.resident.start_exception = RuntimeError("start failed")

    result = harness.coordinator().start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.plc.publish_calls) == 1
    ticket, payload = harness.plc.publish_calls[0]
    assert ticket == package.PlcIngressTicket(RUN_IDS[0], 1, 1)
    _assert_error_physical_plc_payload(payload)
    assert harness.plc.reject_calls == []
    assert harness.plc.mark_calls == []


@pytest.mark.parametrize(
    ("boundary", "outcome"),
    [
        ("rejection", PlcMutationDisposition.EXCLUDED),
        ("rejection", PlcMutationDisposition.DUPLICATE_OR_STALE),
        ("rejection", PlcMutationDisposition.INVALID_STATE),
        ("rejection", PlcMutationDisposition.CLOSED),
        ("rejection", object()),
        ("rejection", RuntimeError("reject failed")),
        ("terminal", PlcMutationDisposition.EXCLUDED),
        ("terminal", PlcMutationDisposition.DUPLICATE_OR_STALE),
        ("terminal", PlcMutationDisposition.INVALID_STATE),
        ("terminal", PlcMutationDisposition.CLOSED),
        ("terminal", object()),
        ("terminal", RuntimeError("terminal failed")),
    ],
)
def test_plc_settlement_non_applied_or_exception_is_one_fail_closed_attempt(
    boundary: str,
    outcome,
) -> None:
    harness = Harness()
    if boundary == "rejection":
        harness.captured_freezer.exception = RuntimeError("preactive failed")
        if isinstance(outcome, Exception):
            harness.plc.reject_exception = outcome
        else:
            harness.plc.reject_result = outcome
    else:
        harness.resident.start_exception = RuntimeError("postactive failed")
        if isinstance(outcome, Exception):
            harness.plc.publish_exception = outcome
        else:
            harness.plc.publish_result = outcome

    harness.coordinator().start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    calls = (
        harness.plc.reject_calls
        if boundary == "rejection"
        else harness.plc.publish_calls
    )
    other_calls = (
        harness.plc.publish_calls
        if boundary == "rejection"
        else harness.plc.reject_calls
    )
    assert len(calls) == 1
    assert other_calls == []
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "plc publication"
    ]
    assert len(diagnostics) == 1
    assert len(diagnostics[0].message.encode("utf-8")) <= 512


def test_synchronous_completion_during_start_waits_for_plc_acceptance() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    mark_entered = Event()
    mark_release = Event()
    harness.plc.hooks["mark"].entered = mark_entered
    harness.plc.hooks["mark"].release = mark_release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(
                captured_request(source=TriggerSource.PLC)
            )
        )
    )

    thread.start()
    assert mark_entered.wait(2)
    assert len(harness.resident.register_calls) == 1
    assert "start" in harness.trace
    assert harness.save_dispatcher.calls == []
    assert harness.plc.publish_calls == []
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        assert context.early_capture_outcome is not None
        assert context.activation_ready is False
        assert context.plc_accepted is False

    mark_release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert result_box[0].status is StartStatus.ACCEPTED_ACTIVE
    assert len(harness.plc.mark_calls) == 1
    assert len(harness.save_dispatcher.calls) == 1
    assert harness.trace.index("start") < harness.trace.index("plc_mark")
    assert harness.trace.index("plc_mark") < harness.trace.index("save_submit")


def test_stop_and_shutdown_contenders_racing_plc_start_settle_once() -> None:
    harness = Harness()
    start_entered = Event()
    start_release = Event()
    harness.resident.hooks["start"].entered = start_entered
    harness.resident.hooks["start"].release = start_release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(
                captured_request(source=TriggerSource.PLC)
            )
        )
    )

    thread.start()
    assert start_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    coordinator._terminalize(
        context,
        TerminalConclusion.MANUAL_STOPPED,
        TerminalErrorCategory.MANUAL_STOPPED,
        "user stop raced capture start",
    )
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.SHUTDOWN_CANCELLED,
        "shutdown raced capture start",
    )

    start_release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.persistence.terminal_calls) == 1
    assert len(harness.plc.publish_calls) == 1
    assert harness.plc.reject_calls == []
    assert harness.plc.mark_calls == []


def test_two_plc_runs_use_only_sequencer_ticket_ordering() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    first = coordinator.start_captured(
        captured_request(RUN_IDS[0], source=TriggerSource.PLC)
    )
    second = coordinator.start_captured(
        captured_request(RUN_IDS[1], source=TriggerSource.PLC)
    )

    assert first.status is StartStatus.ACCEPTED_ACTIVE
    assert second.status is StartStatus.ACCEPTED_ACTIVE
    assert [
        ticket.plc_sequence for ticket in harness.plc.mark_calls
    ] == [1, 2]
    assert not hasattr(coordinator, "_plc_sequence")
    assert not hasattr(coordinator, "_plc_active_count")


@pytest.mark.parametrize(
    ("stage", "plc"),
    [
        ("freeze", False),
        ("reserve", False),
        ("active", False),
        ("plc_register", True),
        ("mark", True),
        ("plc_reject", True),
        ("plc_publish", True),
        ("register", False),
        ("history", False),
        ("start", False),
    ],
)
def test_captured_coordinator_lock_is_free_during_each_external_call(
    stage: str, plc: bool
) -> None:
    harness = Harness()
    entered = Event()
    release = Event()
    if stage == "freeze":
        hook = harness.captured_freezer.hook
    elif stage in harness.resident.hooks:
        hook = harness.resident.hooks[stage]
    elif stage == "active":
        hook = harness.persistence.hooks["active"]
    elif stage == "mark":
        hook = harness.plc.hooks["mark"]
    elif stage.startswith("plc_"):
        hook = harness.plc.hooks[stage.removeprefix("plc_")]
    else:
        hook = harness.history.hook
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    if stage == "plc_reject":
        harness.captured_freezer.exception = RuntimeError("freeze failed")
    elif stage == "plc_publish":
        harness.resident.start_exception = RuntimeError("start failed")
    request = captured_request(
        source=TriggerSource.PLC if plc else TriggerSource.MANUAL
    )
    run_thread = Thread(target=lambda: coordinator.start_captured(request))
    run_thread.start()
    assert entered.wait(2)

    snapshot_done = Event()
    snapshot_thread = Thread(
        target=lambda: (
            coordinator.snapshot(),
            snapshot_done.set(),
        )
    )
    snapshot_thread.start()
    assert snapshot_done.wait(2)

    release.set()
    snapshot_thread.join(2)
    run_thread.join(2)
    assert not run_thread.is_alive()


def test_captured_recording_history_holds_terminal_contender_until_reconcile() -> None:
    harness = Harness()
    entered = Event()
    release = Event()
    harness.history.hook.entered = entered
    harness.history.hook.release = release
    coordinator = harness.coordinator()
    thread = Thread(
        target=lambda: coordinator.start_captured(captured_request())
    )
    thread.start()
    assert entered.wait(2)
    reservation = harness.resident.register_calls[0]
    outcome = CaptureOutcome(
        run_id=reservation.run_id,
        reservation=reservation,
        status=CaptureOutcomeStatus.FAILED,
        audio=None,
        error_category=TerminalErrorCategory.CAPTURE_FAILED,
        reason="capture failed",
    )

    callback_thread = Thread(target=lambda: harness.resident.complete(outcome))
    callback_thread.start()
    callback_thread.join(2)
    assert harness.persistence.terminal_calls == []
    assert harness.resident.start_calls == []

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(harness.persistence.terminal_calls) == 1


def test_save_imported_ingress_skips_resident_and_plc() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_imported(imported_request())

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert result.phase is CoordinatorPhase.SAVING
    assert harness.resident.reserve_calls == []
    assert harness.plc.register_calls == []
    assert harness.trace == [
        "freeze",
        "create_active",
        "project:WAITING_SAVE",
        "history:WAITING_SAVE",
        "save_submit",
    ]
    request, _ = harness.save_dispatcher.calls[0]
    assert request.source_kind is AudioSaveSourceKind.IMPORTED_SOURCE


def test_save_synchronous_capture_completion_is_processed_after_start() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert result.phase is CoordinatorPhase.SAVING
    assert harness.trace.index("start") < harness.trace.index(
        "history:WAITING_SAVE"
    )
    assert harness.trace.index("history:WAITING_SAVE") < harness.trace.index(
        "save_submit"
    )


def test_start_exception_wins_over_synchronous_capture_completion() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.resident.start_exception = RuntimeError("late start error")
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is TerminalErrorCategory.CAPTURE_START_FAILED
    assert harness.save_dispatcher.calls == []
    assert len(harness.persistence.terminal_calls) == 1


def test_save_duplicate_or_mismatched_capture_outcome_is_ignored() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())
    reservation = harness.resident.register_calls[0]
    mismatched = replace(reservation, reservation_id="different-reservation")

    harness.resident._sinks[reservation.reservation_id](
        completed_capture(mismatched)
    )
    assert harness.save_dispatcher.calls == []

    harness.resident.complete(completed_capture(reservation))
    harness.resident.complete(completed_capture(reservation))
    assert len(harness.save_dispatcher.calls) == 1


def test_save_callback_before_receipt_is_held_until_receipt_reconcile() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    coordinator = harness.coordinator()

    coordinator.start_captured(captured_request())

    assert harness.trace.index("save_submit") < harness.trace.index(
        "create_queued"
    )
    assert len(harness.persistence.queued_calls) == 1


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (SaveSubmitStatus.SATURATED, TerminalErrorCategory.AUDIO_SAVE_SATURATED),
        (SaveSubmitStatus.CLOSED, TerminalErrorCategory.AUDIO_SAVE_FAILED),
        (SaveSubmitStatus.INVALID, TerminalErrorCategory.AUDIO_SAVE_FAILED),
    ],
)
def test_save_rejected_submission_terminalizes_once(
    status: SaveSubmitStatus, category: TerminalErrorCategory
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.status = status
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is category
    assert len(harness.persistence.terminal_calls) == 1


def test_save_submission_exception_terminalizes_once() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.exception = RuntimeError("dispatcher unavailable")
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is TerminalErrorCategory.AUDIO_SAVE_FAILED
    assert len(harness.persistence.terminal_calls) == 1


def test_save_malformed_accepted_receipt_cancels_submitted_child_first() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    submit = harness.save_dispatcher.try_submit
    harness.save_dispatcher.try_submit = lambda request, sink: replace(
        submit(request, sink),
        run_id=RUN_IDS[1],
        save_job_id=SAVE_JOB_IDS[1],
    )
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    submitted_request = harness.save_dispatcher.calls[0][0]
    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert harness.save_dispatcher.cancel_calls == [
        (submitted_request.run_id, submitted_request.save_job_id)
    ]
    assert harness.trace.index("save_cancel") < harness.trace.index(
        "commit_terminal"
    )
    assert len(harness.persistence.terminal_calls) == 1
    assert coordinator._save_jobs == {}


def test_duplicate_save_job_id_does_not_overwrite_provisional_first_owner(
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_job_ids.values.clear()
    harness.save_job_ids.values.extend(
        (SAVE_JOB_IDS[0], SAVE_JOB_IDS[0])
    )
    first_entered = Event()
    first_release = Event()
    original_submit = harness.save_dispatcher.try_submit

    def block_first_submit(request, sink):
        if request.run_id == RUN_IDS[0]:
            first_entered.set()
            assert first_release.wait(5)
        return original_submit(request, sink)

    harness.save_dispatcher.try_submit = block_first_submit
    coordinator = harness.coordinator()
    first_results = []
    second_results = []
    first_thread = Thread(
        target=lambda: first_results.append(
            coordinator.start_captured(captured_request(RUN_IDS[0]))
        )
    )
    second_thread = Thread(
        target=lambda: second_results.append(
            coordinator.start_captured(captured_request(RUN_IDS[1]))
        )
    )

    first_thread.start()
    assert first_entered.wait(2)
    second_thread.start()
    second_thread.join(2)
    with coordinator._lock:
        first_context = coordinator._runs[RUN_IDS[0]]
        exact_owner_while_provisional = (
            coordinator._save_jobs.get(SAVE_JOB_IDS[0]) is first_context
        )
    second_finished_before_first = not second_thread.is_alive()

    first_release.set()
    first_thread.join(2)
    second_thread.join(2)
    first_request, first_sink = next(
        call
        for call in harness.save_dispatcher.calls
        if call[0].run_id == RUN_IDS[0]
    )
    first_sink(saved_outcome(first_request))
    with coordinator._lock:
        first_accepted_its_outcome = (
            coordinator._runs[RUN_IDS[0]].phase
            is CoordinatorPhase.ANALYSIS_PENDING
        )

    assert second_finished_before_first
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert exact_owner_while_provisional
    assert len(harness.save_dispatcher.calls) == 1
    assert second_results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert (
        second_results[0].terminal.error_category
        is TerminalErrorCategory.AUDIO_SAVE_FAILED
    )
    assert first_accepted_its_outcome
    assert harness.scheduler.calls[0][0].run_id == RUN_IDS[0]


def test_duplicate_attempt_id_does_not_overwrite_provisional_first_owner(
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    harness.attempt_ids.values.clear()
    harness.attempt_ids.values.extend((ATTEMPT_IDS[0], ATTEMPT_IDS[0]))
    first_entered = Event()
    first_release = Event()
    original_create_queued = harness.persistence.create_queued_attempt

    def block_first_queued(prepared):
        if prepared.run_id == RUN_IDS[0]:
            first_entered.set()
            assert first_release.wait(5)
        return original_create_queued(prepared)

    harness.persistence.create_queued_attempt = block_first_queued
    coordinator = harness.coordinator()
    first_results = []
    second_results = []
    first_thread = Thread(
        target=lambda: first_results.append(
            coordinator.start_captured(captured_request(RUN_IDS[0]))
        )
    )
    second_thread = Thread(
        target=lambda: second_results.append(
            coordinator.start_captured(captured_request(RUN_IDS[1]))
        )
    )

    first_thread.start()
    assert first_entered.wait(2)
    second_thread.start()
    second_thread.join(2)
    with coordinator._lock:
        first_context = coordinator._runs[RUN_IDS[0]]
        first_provisional_identity_is_exact = (
            first_context.attempt_id == ATTEMPT_IDS[0]
        )
        no_second_committed_owner = (
            coordinator._attempts.get(ATTEMPT_IDS[0]) is None
        )
    second_finished_before_first = not second_thread.is_alive()

    first_release.set()
    first_thread.join(2)
    second_thread.join(2)
    with coordinator._lock:
        exact_committed_owner = (
            coordinator._attempts.get(ATTEMPT_IDS[0]) is first_context
        )
    first_job, first_sink = next(
        call
        for call in harness.scheduler.calls
        if call[0].run_id == RUN_IDS[0]
    )
    first_sink(completed_analysis(first_job))

    assert second_finished_before_first
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_provisional_identity_is_exact
    assert no_second_committed_owner
    assert exact_committed_owner
    assert len(harness.persistence.queued_calls) == 1
    assert second_results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert (
        second_results[0].terminal.error_category
        is TerminalErrorCategory.ATTEMPT_ID_FAILED
    )
    assert first_results[0].status is StartStatus.ACCEPTED_ACTIVE
    assert harness.persistence.terminal_calls[-1].run_id == RUN_IDS[0]
    assert (
        harness.persistence.terminal_calls[-1].conclusion
        is TerminalConclusion.OK
    )


def test_save_accepted_dispatcher_owns_full_audio() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()

    coordinator.start_captured(captured_request())

    context = coordinator._runs[RUN_IDS[0]]
    assert context.phase is CoordinatorPhase.SAVING
    assert context.early_capture_outcome is None
    assert context.save_request is None


def _successful_pipeline_harness(*, plc: bool = False) -> tuple[Harness, object]:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    coordinator = harness.coordinator()
    request = captured_request(
        source=TriggerSource.PLC if plc else TriggerSource.MANUAL
    )
    return harness, (coordinator, request)


def test_scheduler_queued_commit_and_waiting_history_precede_submit() -> None:
    harness, pair = _successful_pipeline_harness()
    coordinator, request = pair

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert result.phase is CoordinatorPhase.ANALYSIS_PENDING
    assert harness.trace.index("create_queued") < harness.trace.index(
        "history:WAITING_ANALYSIS"
    )
    assert harness.trace.index("history:WAITING_ANALYSIS") < harness.trace.index(
        "scheduler_submit"
    )
    assert harness.trace.index("scheduler_submit") < harness.trace.index(
        "history:ANALYZING"
    )


def test_scheduler_receives_only_lightweight_audio_and_frozen_input() -> None:
    harness, pair = _successful_pipeline_harness()
    coordinator, request = pair

    coordinator.start_captured(request)

    job, _ = harness.scheduler.calls[0]
    assert isinstance(job, AnalysisJob)
    assert job.audio == harness.persistence.queued_calls[0].audio
    assert job.analysis_input == harness.persistence.queued_calls[0].analysis_input
    assert not hasattr(job, "interleaved_bytes")
    assert harness.scheduler.before_audio_load_calls == []


def test_scheduler_early_outcome_waits_for_analyzing_history() -> None:
    harness, pair = _successful_pipeline_harness()
    harness.scheduler.synchronous_outcome = completed_analysis
    coordinator, request = pair

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert harness.trace.index("scheduler_submit") < harness.trace.index(
        "history:ANALYZING"
    )
    assert harness.trace.index("history:ANALYZING") < harness.trace.index(
        "commit_terminal"
    )


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (
            SchedulerSubmitStatus.QUEUE_FULL,
            TerminalErrorCategory.ANALYSIS_QUEUE_FULL,
        ),
        (
            SchedulerSubmitStatus.NOT_ACCEPTING,
            TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING,
        ),
        (
            SchedulerSubmitStatus.INVALID,
            TerminalErrorCategory.ANALYSIS_INVALID,
        ),
    ],
)
def test_scheduler_rejected_receipt_terminalizes_once(
    status: SchedulerSubmitStatus, category: TerminalErrorCategory
) -> None:
    harness, pair = _successful_pipeline_harness()
    harness.scheduler.status = status
    coordinator, request = pair

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert result.terminal.error_category is category
    assert len(harness.persistence.terminal_calls) == 1


def test_scheduler_submit_exception_terminalizes_once() -> None:
    harness, pair = _successful_pipeline_harness()
    harness.scheduler.exception = RuntimeError("scheduler unavailable")
    coordinator, request = pair

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert (
        result.terminal.error_category
        is TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING
    )
    assert len(harness.persistence.terminal_calls) == 1


@pytest.mark.parametrize(
    "status",
    [
        SchedulerSubmitStatus.QUEUE_FULL,
        SchedulerSubmitStatus.NOT_ACCEPTING,
        SchedulerSubmitStatus.INVALID,
    ],
)
def test_scheduler_rejected_receipt_diagnoses_contradicted_early_outcome(
    status: SchedulerSubmitStatus,
) -> None:
    harness, pair = _successful_pipeline_harness()
    harness.scheduler.synchronous_outcome = completed_analysis
    harness.scheduler.status = status
    coordinator, request = pair

    class DiagnosticsBeforeTerminal:
        def __init__(self) -> None:
            self.calls = []

        def record(self, fact) -> None:
            assert not coordinator._lock._is_owned()
            assert harness.persistence.terminal_calls == []
            with coordinator._lock:
                context = coordinator._attempts[fact.attempt_id]
                assert context.early_analysis_outcome is not None
            self.calls.append(fact)

    diagnostics = DiagnosticsBeforeTerminal()
    coordinator._diagnostics = diagnostics

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert len(diagnostics.calls) == 1
    assert diagnostics.calls[0].stage == "analysis callback ignored"
    assert (
        diagnostics.calls[0].attempt_id
        == harness.scheduler.calls[0][0].attempt_id
    )
    assert "rejected Scheduler receipt" in diagnostics.calls[0].message
    assert len(diagnostics.calls[0].message.encode("utf-8")) <= 512


def test_scheduler_mismatched_receipt_terminalizes_as_invalid() -> None:
    harness, pair = _successful_pipeline_harness()
    submit = harness.scheduler.submit
    harness.scheduler.submit = lambda job, sink: replace(
        submit(job, sink), audio_id=AUDIO_IDS[1]
    )
    coordinator, request = pair

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert (
        result.terminal.error_category
        is TerminalErrorCategory.ANALYSIS_INVALID
    )
    assert len(harness.persistence.terminal_calls) == 1


def test_scheduler_malformed_accepted_receipt_cancels_submitted_child_first() -> None:
    harness, pair = _successful_pipeline_harness()
    submit = harness.scheduler.submit
    harness.scheduler.submit = lambda job, sink: replace(
        submit(job, sink),
        run_id=RUN_IDS[1],
        attempt_id=ATTEMPT_IDS[1],
    )
    coordinator, request = pair

    result = coordinator.start_captured(request)

    submitted_job = harness.scheduler.calls[0][0]
    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert harness.scheduler.cancel_calls == [
        (
            submitted_job.run_id,
            submitted_job.attempt_id,
            "Scheduler returned mismatched submission identity",
        )
    ]
    assert harness.trace.index("scheduler_cancel") < harness.trace.index(
        "commit_terminal"
    )
    assert len(harness.persistence.terminal_calls) == 1
    assert coordinator._attempts == {}


def test_scheduler_mismatched_then_exact_outcome_terminalizes_once() -> None:
    harness, pair = _successful_pipeline_harness()
    coordinator, request = pair
    coordinator.start_captured(request)
    job, sink = harness.scheduler.calls[0]
    mismatched = replace(completed_analysis(job), audio_id=AUDIO_IDS[1])

    sink(mismatched)
    assert harness.persistence.terminal_calls == []

    exact = completed_analysis(job)
    sink(exact)
    sink(exact)
    assert len(harness.persistence.terminal_calls) == 1


def test_terminal_commit_precedes_history_and_plc() -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    harness.scheduler.synchronous_outcome = completed_analysis
    coordinator, request = pair

    coordinator.start_captured(request)

    assert harness.trace.index("commit_terminal") < harness.trace.index(
        "history:RUN_TERMINATED"
    )
    assert harness.trace.index("commit_terminal") < harness.trace.index(
        "plc_publish"
    )


def test_terminal_commit_failure_skips_history_and_settles_plc_once() -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    harness.scheduler.synchronous_outcome = completed_analysis
    harness.persistence.terminal_results.append(CommitResult.REJECTED)
    coordinator, request = pair

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert not [
        call
        for call in harness.history.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(harness.plc.publish_calls) == 1
    settlement = harness.plc.publish_calls[0][1]
    assert (
        settlement.error_category
        is TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED
    )


def test_terminal_history_exception_does_not_suppress_plc() -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    coordinator, request = pair
    coordinator.start_captured(request)
    harness.history.exception = RuntimeError("history failed")
    job, sink = harness.scheduler.calls[0]

    sink(completed_analysis(job))

    assert len(harness.persistence.terminal_calls) == 1
    assert len(harness.plc.publish_calls) == 1


def test_terminal_plc_exception_does_not_roll_back_history() -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    coordinator, request = pair
    coordinator.start_captured(request)
    harness.plc.publish_exception = RuntimeError("PLC failed")
    job, sink = harness.scheduler.calls[0]

    sink(completed_analysis(job))

    assert len(harness.persistence.terminal_calls) == 1
    assert len(
        [
            event
            for event in harness.history.calls
            if event.kind is HistoryFactKind.RUN_TERMINATED
        ]
    ) == 1


def test_terminal_publishing_stays_visible_until_plc_call_returns() -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    coordinator, request = pair
    coordinator.start_captured(request)
    entered = Event()
    release = Event()
    harness.plc.hooks["publish"].entered = entered
    harness.plc.hooks["publish"].release = release
    job, sink = harness.scheduler.calls[0]
    thread = Thread(target=lambda: sink(completed_analysis(job)))
    thread.start()
    assert entered.wait(2)

    assert coordinator.snapshot().pending_publication_run_ids == (RUN_IDS[0],)

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert coordinator.snapshot().pending_publication_run_ids == ()


def test_terminal_publishing_replaces_active_context_with_fixed_state() -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    coordinator, request = pair
    coordinator.start_captured(request)
    entered = Event()
    release = Event()
    harness.plc.hooks["publish"].entered = entered
    harness.plc.hooks["publish"].release = release
    job, sink = harness.scheduler.calls[0]
    thread = Thread(target=lambda: sink(completed_analysis(job)))

    thread.start()
    assert entered.wait(2)
    publication = coordinator._runs[RUN_IDS[0]]

    assert publication.run_id == RUN_IDS[0]
    assert publication.ticket is not None
    assert not publication.history_in_flight
    assert publication.plc_in_flight
    for active_field in (
        "request",
        "frozen",
        "reservation",
        "persisted_audio",
        "attempt_id",
        "attempt_kind",
        "save_request",
        "save_metadata",
        "early_capture_outcome",
        "early_save_outcome",
        "early_analysis_outcome",
    ):
        assert not hasattr(publication, active_field)

    release.set()
    thread.join(2)
    assert not thread.is_alive()


class _UnlockedDiagnostics:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.calls = []

    def record(self, fact) -> None:
        assert not self.coordinator._lock._is_owned()
        self.calls.append(fact)


class _LockCheckedDict(dict):
    def __init__(self, coordinator, values) -> None:
        super().__init__(values)
        self.coordinator = coordinator

    def get(self, key, default=None):
        assert self.coordinator._lock._is_owned()
        return super().get(key, default)


def test_ignored_capture_callbacks_diagnose_once_outside_lock() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    diagnostics = _UnlockedDiagnostics(coordinator)
    coordinator._diagnostics = diagnostics
    coordinator.start_captured(captured_request())
    reservation = harness.resident.register_calls[0]
    exact = completed_capture(reservation)

    coordinator._on_capture_outcome(RUN_IDS[1], exact)
    harness.resident._sinks[reservation.reservation_id](
        completed_capture(
            replace(reservation, reservation_id="wrong-reservation")
        )
    )
    harness.resident.complete(exact)
    harness.resident.complete(exact)

    assert len(diagnostics.calls) == 3
    assert [fact.stage for fact in diagnostics.calls] == [
        "capture callback ignored",
    ] * 3
    assert all(len(fact.message.encode("utf-8")) <= 512 for fact in diagnostics.calls)


def test_ignored_save_callbacks_diagnose_once_outside_lock() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()
    diagnostics = _UnlockedDiagnostics(coordinator)
    coordinator._diagnostics = diagnostics
    coordinator.start_captured(captured_request())
    request, sink = harness.save_dispatcher.calls[0]
    mismatched = AudioSaveOutcome(
        save_job_id=request.save_job_id,
        run_id=RUN_IDS[1],
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        status=AudioSaveStatus.FAILED,
        persisted_audio=None,
        error_category=TerminalErrorCategory.AUDIO_SAVE_FAILED,
        reason="wrong run",
    )
    exact = saved_outcome(request)

    sink(mismatched)
    sink(exact)
    sink(exact)

    assert len(diagnostics.calls) == 2
    assert [fact.stage for fact in diagnostics.calls] == [
        "save callback ignored",
    ] * 2
    assert all(fact.save_job_id == request.save_job_id for fact in diagnostics.calls)


def test_coordinator_consumes_one_save_failure_as_only_terminal_authority(
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()
    result = coordinator.start_captured(captured_request())
    assert result.phase is CoordinatorPhase.SAVING
    request, sink = harness.save_dispatcher.calls[0]
    failure = AudioSaveOutcome(
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        status=AudioSaveStatus.FAILED,
        persisted_audio=None,
        error_category=TerminalErrorCategory.AUDIO_SAVE_FAILED,
        reason="persistence failed",
    )

    sink(failure)
    sink(failure)

    assert len(harness.persistence.terminal_calls) == 1
    terminal = harness.persistence.terminal_calls[0]
    assert terminal.error_category is TerminalErrorCategory.AUDIO_SAVE_FAILED
    assert terminal.reason == "persistence failed"
    assert RUN_IDS[0] not in coordinator.snapshot().accepted_active_run_ids


def test_ignored_analysis_callbacks_diagnose_once_outside_lock() -> None:
    harness, pair = _successful_pipeline_harness()
    coordinator, request = pair
    diagnostics = _UnlockedDiagnostics(coordinator)
    coordinator._diagnostics = diagnostics
    coordinator.start_captured(request)
    job, sink = harness.scheduler.calls[0]
    exact = completed_analysis(job)

    sink(replace(exact, audio_id=AUDIO_IDS[1]))
    sink(exact)
    sink(exact)

    assert len(diagnostics.calls) == 2
    assert [fact.stage for fact in diagnostics.calls] == [
        "analysis callback ignored",
    ] * 2
    assert all(fact.attempt_id == job.attempt_id for fact in diagnostics.calls)


@pytest.mark.parametrize("callback_kind", ["capture", "save", "analysis"])
def test_callback_exception_handler_reads_indexes_only_under_lock(
    callback_kind: str,
) -> None:
    harness = Harness()
    if callback_kind in {"save", "analysis"}:
        harness.resident.synchronous_outcome = completed_capture
    if callback_kind == "analysis":
        harness.save_dispatcher.synchronous_outcome = saved_outcome
    coordinator = harness.coordinator()
    diagnostics = _UnlockedDiagnostics(coordinator)
    coordinator._diagnostics = diagnostics
    coordinator.start_captured(captured_request())

    def fail(*_args):
        raise RuntimeError("callback processing failed")

    if callback_kind == "capture":
        reservation = harness.resident.register_calls[0]
        coordinator._process_capture_outcome = fail
        coordinator._runs = _LockCheckedDict(coordinator, coordinator._runs)
        harness.resident.complete(completed_capture(reservation))
    elif callback_kind == "save":
        request, sink = harness.save_dispatcher.calls[0]
        coordinator._process_save_outcome = fail
        coordinator._save_jobs = _LockCheckedDict(
            coordinator, coordinator._save_jobs
        )
        sink(saved_outcome(request))
    else:
        job, sink = harness.scheduler.calls[0]
        coordinator._process_analysis_outcome = fail
        coordinator._attempts = _LockCheckedDict(
            coordinator, coordinator._attempts
        )
        sink(completed_analysis(job))

    assert len(diagnostics.calls) == 1


@pytest.mark.parametrize(
    ("lifecycle", "registration_disposition", "expected_settlements"),
    [
        (
            CoordinatorLifecycle.STOPPING,
            PlcMutationDisposition.APPLIED,
            1,
        ),
        (
            CoordinatorLifecycle.STOPPING,
            PlcMutationDisposition.INVALID_STATE,
            0,
        ),
        (
            CoordinatorLifecycle.CLOSED,
            PlcMutationDisposition.APPLIED,
            1,
        ),
        (
            CoordinatorLifecycle.CLOSED,
            PlcMutationDisposition.CLOSED,
            0,
        ),
    ],
)
def test_plc_registration_stop_shutdown_contender_settles_owned_ticket_only(
    lifecycle: CoordinatorLifecycle,
    registration_disposition: PlcMutationDisposition,
    expected_settlements: int,
) -> None:
    harness = Harness()
    harness.plc.registration_disposition = registration_disposition
    entered = Event()
    release = Event()
    harness.plc.hooks["register"].entered = entered
    harness.plc.hooks["register"].release = release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(
                captured_request(source=TriggerSource.PLC)
            )
        )
    )

    thread.start()
    assert entered.wait(2)
    snapshot_done = Event()
    snapshot_thread = Thread(
        target=lambda: (coordinator.snapshot(), snapshot_done.set())
    )
    snapshot_thread.start()
    assert snapshot_done.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        coordinator._lifecycle = lifecycle
        context.cancel_requested = True
        if lifecycle is CoordinatorLifecycle.CLOSED:
            context.history_allowed = False

    release.set()
    thread.join(2)
    snapshot_thread.join(2)

    assert not thread.is_alive()
    assert harness.captured_freezer.calls == []
    assert len(harness.plc.reject_calls) == expected_settlements
    assert harness.plc.publish_calls == []
    if lifecycle is CoordinatorLifecycle.CLOSED:
        assert harness.history.calls == []


_POST_ACCEPT_CONTENDER_STAGES = (
    "captured_active_commit",
    "imported_active_commit",
    "capture_register",
    "recording_projector",
    "recording_sink",
    "capture_start",
    "waiting_save_projector",
    "waiting_save_sink",
    "save_job_factory",
    "save_submit",
    "attempt_id_factory",
    "queued_commit",
    "waiting_analysis_projector",
    "waiting_analysis_sink",
    "scheduler_submit",
    "analyzing_projector",
    "analyzing_sink",
    "terminal_commit",
)


def _configure_post_accept_contender_stage(harness: Harness, stage: str) -> None:
    if stage in {
        "waiting_save_projector",
        "waiting_save_sink",
        "save_job_factory",
        "save_submit",
        "attempt_id_factory",
        "queued_commit",
        "waiting_analysis_projector",
        "waiting_analysis_sink",
        "scheduler_submit",
        "analyzing_projector",
        "analyzing_sink",
        "terminal_commit",
    }:
        harness.resident.synchronous_outcome = completed_capture
    if stage in {
        "attempt_id_factory",
        "queued_commit",
        "waiting_analysis_projector",
        "waiting_analysis_sink",
        "scheduler_submit",
        "analyzing_projector",
        "analyzing_sink",
        "terminal_commit",
    }:
        harness.save_dispatcher.synchronous_outcome = saved_outcome
    if stage == "terminal_commit":
        harness.scheduler.synchronous_outcome = completed_analysis


def _post_accept_stage_hook(harness: Harness, stage: str):
    return {
        "captured_active_commit": harness.persistence.hooks["active"],
        "imported_active_commit": harness.persistence.hooks["active"],
        "capture_register": harness.resident.hooks["register"],
        "recording_projector": harness.projector.hooks[
            HistoryFactKind.RECORDING
        ],
        "recording_sink": harness.history.hooks[HistoryFactKind.RECORDING],
        "capture_start": harness.resident.hooks["start"],
        "waiting_save_projector": harness.projector.hooks[
            HistoryFactKind.WAITING_SAVE
        ],
        "waiting_save_sink": harness.history.hooks[
            HistoryFactKind.WAITING_SAVE
        ],
        "save_job_factory": harness.save_job_ids.hook,
        "save_submit": harness.save_dispatcher.hook,
        "attempt_id_factory": harness.attempt_ids.hook,
        "queued_commit": harness.persistence.hooks["queued"],
        "waiting_analysis_projector": harness.projector.hooks[
            HistoryFactKind.WAITING_ANALYSIS
        ],
        "waiting_analysis_sink": harness.history.hooks[
            HistoryFactKind.WAITING_ANALYSIS
        ],
        "scheduler_submit": harness.scheduler.hook,
        "analyzing_projector": harness.projector.hooks[
            HistoryFactKind.ANALYZING
        ],
        "analyzing_sink": harness.history.hooks[HistoryFactKind.ANALYZING],
        "terminal_commit": harness.persistence.hooks["terminal"],
    }[stage]


@pytest.mark.parametrize("stage", _POST_ACCEPT_CONTENDER_STAGES)
def test_post_accept_action_matrix_consumes_contender_before_next_port(
    stage: str,
) -> None:
    harness = Harness()
    _configure_post_accept_contender_stage(harness, stage)
    hook = _post_accept_stage_hook(harness, stage)
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    if stage == "imported_active_commit":
        start = lambda: coordinator.start_imported(imported_request())
    else:
        start = lambda: coordinator.start_captured(captured_request())
    thread = Thread(target=lambda: result_box.append(start()))

    thread.start()
    assert entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        (
            TerminalErrorCategory.AUDIO_SAVE_FAILED
            if stage == "imported_active_commit"
            else (
                TerminalErrorCategory.ANALYSIS_FAILED
                if context.attempt_id is not None
                else TerminalErrorCategory.CAPTURE_FAILED
            )
        ),
        f"terminal contender at {stage}",
    )
    terminal_calls_while_blocked = len(harness.persistence.terminal_calls)
    if stage == "terminal_commit":
        assert terminal_calls_while_blocked == 1
    else:
        assert terminal_calls_while_blocked == 0

    release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert len(harness.persistence.terminal_calls) == 1
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    forbidden_after_stage = {
        "captured_active_commit": "register",
        "imported_active_commit": "save_submit",
        "capture_register": "project:RECORDING",
        "recording_projector": "history:RECORDING",
        "recording_sink": "start",
        "capture_start": "project:WAITING_SAVE",
        "waiting_save_projector": "history:WAITING_SAVE",
        "waiting_save_sink": "save_submit",
        "save_job_factory": "save_submit",
        "save_submit": "create_queued",
        "attempt_id_factory": "create_queued",
        "queued_commit": "project:WAITING_ANALYSIS",
        "waiting_analysis_projector": "history:WAITING_ANALYSIS",
        "waiting_analysis_sink": "scheduler_submit",
        "scheduler_submit": "project:ANALYZING",
        "analyzing_projector": "history:ANALYZING",
    }.get(stage)
    if forbidden_after_stage is not None:
        assert forbidden_after_stage not in harness.trace


def test_blocked_terminal_wall_clock_releases_lock_and_reconciles_once() -> None:
    harness = Harness()
    harness.resident.register_exception = RuntimeError("register failed")
    entered = Event()
    release = Event()
    harness.clock.hook.entered = entered
    harness.clock.hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )

    thread.start()
    assert entered.wait(2)
    snapshot_done = Event()
    snapshot_thread = Thread(
        target=lambda: (coordinator.snapshot(), snapshot_done.set())
    )
    snapshot_thread.start()
    lock_was_free = snapshot_done.wait(2)
    if lock_was_free:
        with coordinator._lock:
            context = coordinator._runs[RUN_IDS[0]]
            assert context.action.kind is ExternalActionKind.TERMINAL_COMMIT
        coordinator._terminalize(
            context,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.CAPTURE_FAILED,
            "duplicate while terminal clock is blocked",
        )
        assert harness.persistence.terminal_calls == []

    release.set()
    thread.join(2)
    snapshot_thread.join(2)

    assert lock_was_free
    assert not thread.is_alive()
    assert len(harness.persistence.terminal_calls) == 1
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL


def test_blocked_terminal_clock_makes_save_success_stale_before_action_overwrite(
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()
    assert (
        coordinator.start_captured(captured_request()).phase
        is CoordinatorPhase.SAVING
    )
    save_request, save_sink = harness.save_dispatcher.calls[0]
    attempt_ids_before = tuple(harness.attempt_ids.values)
    clock_entered = Event()
    clock_release = Event()
    harness.clock.hook.entered = clock_entered
    harness.clock.hook.release = clock_release
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        save_job_id = context.save_job_id
    terminal_thread = Thread(
        target=lambda: coordinator._terminalize(
            context,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.AUDIO_SAVE_FAILED,
            "terminal while clock is blocked",
        )
    )

    terminal_thread.start()
    assert clock_entered.wait(2)
    with coordinator._lock:
        clock_action = context.action
    save_sink(saved_outcome(save_request))
    with coordinator._lock:
        action_was_not_overwritten = context.action is clock_action
        authority_remained_frozen = (
            context.save_child_accepted
            and context.save_job_id == save_job_id
        )
        terminal_was_claimed = context.terminal_claimed

    clock_release.set()
    cancel_started = cancel_entered.wait(2)
    if cancel_started:
        assert harness.persistence.terminal_calls == []
        cancel_release.set()
    terminal_thread.join(2)

    assert action_was_not_overwritten
    assert authority_remained_frozen
    assert terminal_was_claimed
    assert tuple(harness.attempt_ids.values) == attempt_ids_before
    assert [
        fact.stage
        for fact in harness.diagnostics.calls
        if fact.stage == "save callback ignored"
    ] == ["save callback ignored"]
    assert cancel_started
    assert not terminal_thread.is_alive()
    assert len(harness.save_dispatcher.cancel_calls) == 1
    assert harness.save_dispatcher.cancel_calls[0][1] == save_job_id
    assert (
        harness.trace.index("save_cancel")
        < harness.trace.index("commit_terminal")
    )
    assert len(harness.persistence.terminal_calls) == 1


def test_blocked_terminal_clock_makes_analysis_outcome_stale_before_commit(
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    coordinator = harness.coordinator()
    assert (
        coordinator.start_captured(captured_request()).phase
        is CoordinatorPhase.ANALYSIS_PENDING
    )
    analysis_job, analysis_sink = harness.scheduler.calls[0]
    clock_entered = Event()
    clock_release = Event()
    harness.clock.hook.entered = clock_entered
    harness.clock.hook.release = clock_release
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        attempt_id = context.attempt_id
    terminal_thread = Thread(
        target=lambda: coordinator._terminalize(
            context,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.ANALYSIS_FAILED,
            "terminal while analysis clock is blocked",
        )
    )

    terminal_thread.start()
    assert clock_entered.wait(2)
    with coordinator._lock:
        clock_action = context.action
    analysis_sink(completed_analysis(analysis_job))
    with coordinator._lock:
        action_was_not_overwritten = context.action is clock_action
        authority_remained_frozen = (
            context.scheduler_child_accepted
            and context.attempt_id == attempt_id
        )
        terminal_was_claimed = context.terminal_claimed

    clock_release.set()
    cancel_started = cancel_entered.wait(2)
    if cancel_started:
        assert harness.persistence.terminal_calls == []
        cancel_release.set()
    terminal_thread.join(2)

    assert action_was_not_overwritten
    assert authority_remained_frozen
    assert terminal_was_claimed
    assert [
        fact.stage
        for fact in harness.diagnostics.calls
        if fact.stage == "analysis callback ignored"
    ] == ["analysis callback ignored"]
    assert cancel_started
    assert not terminal_thread.is_alive()
    assert len(harness.scheduler.cancel_calls) == 1
    assert harness.scheduler.cancel_calls[0][1] == attempt_id
    assert (
        harness.trace.index("scheduler_cancel")
        < harness.trace.index("commit_terminal")
    )
    assert len(harness.persistence.terminal_calls) == 1


@pytest.mark.parametrize("authority", ["resident", "save"])
@pytest.mark.parametrize("clock_failure", ["exception", "invalid"])
def test_terminal_clock_failure_cancels_frozen_authority_and_retires(
    authority: str,
    clock_failure: str,
) -> None:
    harness = Harness()
    if authority in {"save", "scheduler"}:
        harness.resident.synchronous_outcome = completed_capture
    if authority == "scheduler":
        harness.save_dispatcher.synchronous_outcome = saved_outcome
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())

    clock_calls = 0
    if clock_failure == "exception":
        def failed_clock():
            nonlocal clock_calls
            clock_calls += 1
            raise RuntimeError("clock unavailable")

        coordinator._wall_clock = failed_clock
    else:
        def invalid_clock():
            nonlocal clock_calls
            clock_calls += 1
            return "not-a-datetime"

        coordinator._wall_clock = invalid_clock
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]

    result = coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        {
            "resident": TerminalErrorCategory.CAPTURE_FAILED,
            "save": TerminalErrorCategory.AUDIO_SAVE_FAILED,
            "scheduler": TerminalErrorCategory.ANALYSIS_FAILED,
        }[authority],
        f"terminal with {authority} authority",
    )

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert (
        result.terminal.error_category
        is {
            "resident": TerminalErrorCategory.CAPTURE_FAILED,
            "save": TerminalErrorCategory.AUDIO_SAVE_FAILED,
            "scheduler": TerminalErrorCategory.ANALYSIS_FAILED,
        }[authority]
    )
    assert "wall clock" in result.terminal.reason
    assert result.terminal.terminal_at == datetime(
        1970, 1, 1, tzinfo=timezone.utc
    )
    assert clock_calls == 1
    assert len(harness.resident.cancel_calls) == (
        1 if authority == "resident" else 0
    )
    assert len(harness.save_dispatcher.cancel_calls) == (
        1 if authority == "save" else 0
    )
    assert len(harness.scheduler.cancel_calls) == (
        1 if authority == "scheduler" else 0
    )
    cancel_trace = {
        "resident": "resident_cancel",
        "save": "save_cancel",
        "scheduler": "scheduler_cancel",
    }[authority]
    assert (
        harness.trace.index(cancel_trace)
        < harness.trace.index("commit_terminal")
    )
    assert len(harness.persistence.terminal_calls) == 1
    assert coordinator._save_jobs == {}
    assert coordinator._attempts == {}
    assert RUN_IDS[0] not in coordinator._runs
    assert coordinator.snapshot().accepted_active_run_ids == ()
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "terminal wall clock"
    ]
    assert len(diagnostics) == 1
    assert (
        diagnostics[0].category
        is TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED
    )
    assert "wall clock" in diagnostics[0].message


def test_terminal_fact_validation_failure_uses_fail_closed_cleanup(
    monkeypatch,
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())
    construction_calls = 0
    original_terminal_post_init = TerminalFact.__post_init__

    def fail_first_terminal_fact(fact):
        nonlocal construction_calls
        construction_calls += 1
        if construction_calls == 1:
            raise ValueError("forced TerminalFact validation failure")
        original_terminal_post_init(fact)

    monkeypatch.setattr(
        TerminalFact, "__post_init__", fail_first_terminal_fact
    )
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]

    result = coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.AUDIO_SAVE_FAILED,
        "save failure",
    )

    assert construction_calls == 2
    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert (
        result.terminal.error_category
        is TerminalErrorCategory.AUDIO_SAVE_FAILED
    )
    assert "terminal fact validation" in result.terminal.reason
    assert result.terminal.terminal_at == datetime(
        2026, 7, 27, 9, 0, tzinfo=timezone.utc
    )
    assert harness.clock.calls == 1
    assert len(harness.save_dispatcher.cancel_calls) == 1
    assert (
        harness.trace.index("save_cancel")
        < harness.trace.index("commit_terminal")
    )
    assert RUN_IDS[0] not in coordinator._runs
    assert coordinator._save_jobs == {}
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "terminal fact validation"
    ]
    assert len(diagnostics) == 1
    assert (
        diagnostics[0].category
        is TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED
    )
    assert "fact validation" in diagnostics[0].message


def test_ticketed_terminal_clock_failure_publishes_once_and_retires() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_captured(
        captured_request(source=TriggerSource.PLC)
    )
    coordinator._wall_clock = lambda: None
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]

    result = coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.CAPTURE_FAILED,
        "capture failed",
    )

    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert (
        result.terminal.error_category
        is TerminalErrorCategory.CAPTURE_FAILED
    )
    assert len(harness.resident.cancel_calls) == 1
    assert harness.plc.reject_calls == []
    assert len(harness.plc.publish_calls) == 1
    assert (
        harness.trace.index("resident_cancel")
        < harness.trace.index("commit_terminal")
        < harness.trace.index("plc_publish")
    )
    assert RUN_IDS[0] not in coordinator._runs


def test_normal_terminal_and_preaccept_never_use_cleanup_sentinel() -> None:
    terminal_harness = Harness()
    terminal_harness.resident.synchronous_outcome = completed_capture
    terminal_harness.save_dispatcher.synchronous_outcome = saved_outcome
    terminal_harness.scheduler.synchronous_outcome = completed_analysis
    terminal_result = terminal_harness.coordinator().start_captured(
        captured_request()
    )
    preaccept_harness = Harness()
    preaccept_harness.captured_freezer.exception = RuntimeError(
        "freeze failed"
    )
    preaccept_result = preaccept_harness.coordinator().start_captured(
        captured_request()
    )
    sentinel = datetime(1970, 1, 1, tzinfo=timezone.utc)

    assert terminal_result.terminal.terminal_at != sentinel
    assert preaccept_result.terminal.terminal_at != sentinel
    assert terminal_harness.clock.calls == 1
    assert preaccept_harness.clock.calls == 1


@pytest.mark.parametrize(
    ("ticketed", "failure_kind"),
    [
        (True, "clock_exception"),
        (False, "invalid_clock"),
        (True, "fact_validation"),
        (False, "fact_validation"),
    ],
)
def test_preaccept_clock_or_fact_failure_settles_and_retires(
    ticketed: bool,
    failure_kind: str,
    monkeypatch,
) -> None:
    harness = Harness()
    harness.captured_freezer.exception = RuntimeError("freeze failed")
    coordinator = harness.coordinator()
    construction_calls = 0
    clock_calls = 0
    if failure_kind == "clock_exception":
        def failed_clock():
            nonlocal clock_calls
            clock_calls += 1
            raise RuntimeError("clock unavailable")

        coordinator._wall_clock = failed_clock
    elif failure_kind == "invalid_clock":
        def invalid_clock():
            nonlocal clock_calls
            clock_calls += 1
            return datetime(2026, 7, 27)

        coordinator._wall_clock = invalid_clock
    else:
        original_preaccept_post_init = (
            PreacceptTerminalFact.__post_init__
        )

        def fail_first_preaccept_fact(fact):
            nonlocal construction_calls
            construction_calls += 1
            if construction_calls == 1:
                raise ValueError(
                    "forced PreacceptTerminalFact validation failure"
                )
            original_preaccept_post_init(fact)

        monkeypatch.setattr(
            PreacceptTerminalFact,
            "__post_init__",
            fail_first_preaccept_fact,
        )
    request = captured_request(
        source=TriggerSource.PLC if ticketed else TriggerSource.MANUAL
    )

    result = coordinator.start_captured(request)

    assert result.status is StartStatus.PREFLIGHT_REJECTED
    assert result.accepted is False
    assert isinstance(result.terminal, PreacceptTerminalFact)
    if failure_kind in {"clock_exception", "invalid_clock"}:
        assert "wall clock" in result.terminal.reason
        assert result.terminal.terminal_at == datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        assert clock_calls == 1
        diagnostic_stage = "preaccept wall clock"
    else:
        assert construction_calls == 2
        assert "preaccept fact validation" in result.terminal.reason
        assert harness.clock.calls == 1
        diagnostic_stage = "preaccept fact validation"
    assert len(harness.plc.reject_calls) == (1 if ticketed else 0)
    assert harness.plc.publish_calls == []
    assert RUN_IDS[0] not in coordinator._runs
    assert coordinator.snapshot().ingress_run_ids == ()
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == diagnostic_stage
    ]
    assert len(diagnostics) == 1
    assert (
        diagnostics[0].category
        is TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED
    )
    assert (
        "wall clock" in diagnostics[0].message
        if "clock" in failure_kind
        else "fact validation" in diagnostics[0].message
    )


def test_blocked_preaccept_wall_clock_is_an_exact_unlocked_action() -> None:
    harness = Harness()
    harness.resident.reserve_results.append(
        CaptureReserveResult(
            status=CaptureReserveStatus.CAPACITY,
            run_id=RUN_IDS[0],
            hardware_id="hardware-1",
            reservation=None,
            reason="capacity",
        )
    )
    entered = Event()
    release = Event()
    harness.clock.hook.entered = entered
    harness.clock.hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )

    thread.start()
    assert entered.wait(2)
    snapshot_done = Event()
    snapshot_thread = Thread(
        target=lambda: (coordinator.snapshot(), snapshot_done.set())
    )
    snapshot_thread.start()
    lock_was_free = snapshot_done.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        action_kind = (
            context.action.kind if context.action is not None else None
        )

    release.set()
    thread.join(2)
    snapshot_thread.join(2)

    assert lock_was_free
    assert action_kind is ExternalActionKind.TERMINAL_COMMIT
    assert result_box[0].status is StartStatus.CAPTURE_CAPACITY


def test_blocked_imported_freezer_releases_coordinator_lock() -> None:
    harness = Harness()
    entered = Event()
    release = Event()
    harness.imported_freezer.hook.entered = entered
    harness.imported_freezer.hook.release = release
    coordinator = harness.coordinator()
    thread = Thread(
        target=lambda: coordinator.start_imported(imported_request())
    )

    thread.start()
    assert entered.wait(2)
    snapshot_done = Event()
    snapshot_thread = Thread(
        target=lambda: (coordinator.snapshot(), snapshot_done.set())
    )
    snapshot_thread.start()

    assert snapshot_done.wait(2)
    release.set()
    thread.join(2)
    snapshot_thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    ("stage", "contender_kind"),
    [
        ("captured_freezer", "terminal"),
        ("captured_freezer", "cancel"),
        ("imported_freezer", "terminal"),
        ("imported_freezer", "cancel"),
        ("resident_reserve", "terminal"),
        ("resident_reserve", "cancel"),
    ],
)
def test_preaccept_contender_after_child_acquisition_stops_before_later_port(
    stage: str,
    contender_kind: str,
) -> None:
    harness = Harness()
    hook = {
        "captured_freezer": harness.captured_freezer.hook,
        "imported_freezer": harness.imported_freezer.hook,
        "resident_reserve": harness.resident.hooks["reserve"],
    }[stage]
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    if stage == "imported_freezer":
        start = lambda: coordinator.start_imported(imported_request())
    else:
        start = lambda: coordinator.start_captured(
            captured_request(source=TriggerSource.PLC)
        )
    thread = Thread(target=lambda: result_box.append(start()))

    thread.start()
    assert entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        if contender_kind == "cancel":
            context.cancel_requested = True
    if contender_kind == "terminal":
        coordinator._terminalize(
            context,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.CAPTURE_FAILED,
            f"terminal contender during {stage}",
        )

    release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert result_box[0].status is StartStatus.PREFLIGHT_REJECTED
    assert result_box[0].accepted is False
    assert harness.persistence.active_calls == []
    assert harness.persistence.terminal_calls == []
    if stage == "captured_freezer":
        assert harness.resident.reserve_calls == []
        assert harness.resident.cancel_calls == []
    elif stage == "resident_reserve":
        assert len(harness.resident.cancel_calls) == 1
        assert (
            harness.trace.index("resident_cancel")
            < harness.trace.index("plc_reject")
        )
    else:
        assert harness.resident.reserve_calls == []
        assert harness.resident.cancel_calls == []
    assert len(harness.plc.reject_calls) == (
        0 if stage == "imported_freezer" else 1
    )


@pytest.mark.parametrize("timing", ["during_submit"])
def test_first_terminal_cancels_accepted_save_child_before_terminal(
    timing: str,
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    submit_entered = Event()
    submit_release = Event()
    if timing == "during_submit":
        harness.save_dispatcher.hook.entered = submit_entered
        harness.save_dispatcher.hook.release = submit_release
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )

    thread.start()
    if timing == "during_submit":
        assert submit_entered.wait(2)
    else:
        thread.join(2)
        assert not thread.is_alive()
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    terminal_thread = None
    if timing == "during_submit":
        coordinator._terminalize(
            context,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.AUDIO_SAVE_FAILED,
            "terminal contender during save submit",
        )
        submit_release.set()
    else:
        terminal_thread = Thread(
            target=lambda: coordinator._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                "terminal contender after save submit",
            )
        )
        terminal_thread.start()

    assert cancel_entered.wait(2)
    assert harness.persistence.terminal_calls == []
    cancel_release.set()
    thread.join(2)
    if terminal_thread is not None:
        terminal_thread.join(2)

    assert not thread.is_alive()
    assert terminal_thread is None or not terminal_thread.is_alive()
    assert len(harness.save_dispatcher.cancel_calls) == 1
    assert harness.scheduler.cancel_calls == []
    assert (
        harness.trace.index("save_cancel")
        < harness.trace.index("commit_terminal")
    )
    assert coordinator._save_jobs == {}
    assert len(harness.persistence.terminal_calls) == 1


@pytest.mark.parametrize("timing", ["during_submit"])
def test_first_terminal_keeps_acquired_attempt_bridge_owned_after_cancel(
    timing: str,
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    submit_entered = Event()
    submit_release = Event()
    if timing == "during_submit":
        harness.scheduler.hook.entered = submit_entered
        harness.scheduler.hook.release = submit_release
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )

    thread.start()
    if timing == "during_submit":
        assert submit_entered.wait(2)
    else:
        thread.join(2)
        assert not thread.is_alive()
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    terminal_thread = None
    if timing == "during_submit":
        coordinator._terminalize(
            context,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.ANALYSIS_FAILED,
            "terminal contender during Scheduler submit",
        )
        submit_release.set()
    else:
        terminal_thread = Thread(
            target=lambda: coordinator._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.ANALYSIS_FAILED,
                "terminal contender after Scheduler submit",
            )
        )
        terminal_thread.start()

    assert cancel_entered.wait(2)
    assert harness.persistence.terminal_calls == []
    cancel_release.set()
    thread.join(2)
    if terminal_thread is not None:
        terminal_thread.join(2)

    assert not thread.is_alive()
    assert terminal_thread is None or not terminal_thread.is_alive()
    assert len(harness.scheduler.cancel_calls) == 1
    assert harness.save_dispatcher.cancel_calls == []
    assert "commit_terminal" not in harness.trace
    assert coordinator._attempts == {ATTEMPT_IDS[0]: context}
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert len(harness.persistence.terminal_calls) == 0


@pytest.mark.parametrize("boundary", ["projector", "sink"])
def test_terminal_history_boundary_is_unlocked_and_precedes_plc(
    boundary: str,
) -> None:
    harness, pair = _successful_pipeline_harness(plc=True)
    coordinator, request = pair
    coordinator.start_captured(request)
    hook = (
        harness.projector.hooks[HistoryFactKind.RUN_TERMINATED]
        if boundary == "projector"
        else harness.history.hooks[HistoryFactKind.RUN_TERMINATED]
    )
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    job, sink = harness.scheduler.calls[0]
    thread = Thread(target=lambda: sink(completed_analysis(job)))

    thread.start()
    assert entered.wait(2)
    snapshot_done = Event()
    snapshot_thread = Thread(
        target=lambda: (coordinator.snapshot(), snapshot_done.set())
    )
    snapshot_thread.start()

    assert snapshot_done.wait(2)
    assert coordinator.snapshot().pending_publication_run_ids == (RUN_IDS[0],)
    assert harness.plc.publish_calls == []

    release.set()
    thread.join(2)
    snapshot_thread.join(2)
    assert not thread.is_alive()
    assert len(harness.plc.publish_calls) == 1


def test_preaccept_plc_reject_boundary_keeps_publication_visible() -> None:
    harness = Harness()
    harness.resident.reserve_results.append(
        CaptureReserveResult(
            status=CaptureReserveStatus.CAPACITY,
            run_id=RUN_IDS[0],
            hardware_id="hardware-1",
            reservation=None,
            reason="capacity",
        )
    )
    entered = Event()
    release = Event()
    harness.plc.hooks["reject"].entered = entered
    harness.plc.hooks["reject"].release = release
    coordinator = harness.coordinator()
    thread = Thread(
        target=lambda: coordinator.start_captured(
            captured_request(source=TriggerSource.PLC)
        )
    )

    thread.start()
    assert entered.wait(2)
    assert coordinator.snapshot().pending_publication_run_ids == (RUN_IDS[0],)
    duplicate = coordinator.start_captured(
        captured_request(source=TriggerSource.PLC)
    )

    assert duplicate.status is StartStatus.DUPLICATE_RUN_ID
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(harness.plc.reject_calls) == 1


def test_resident_permit_release_precedes_waiting_save_and_submit() -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    coordinator = harness.coordinator()

    coordinator.start_captured(captured_request())

    assert harness.trace.index("resident_permit_released") < harness.trace.index(
        "project:WAITING_SAVE"
    )
    assert harness.trace.index("resident_permit_released") < harness.trace.index(
        "save_submit"
    )


@pytest.mark.parametrize(
    "target_kind",
    [
        HistoryFactKind.RECORDING,
        HistoryFactKind.WAITING_SAVE,
        HistoryFactKind.WAITING_ANALYSIS,
    ],
)
def test_post_receipt_history_handoff_cannot_overwrite_terminal_action(
    target_kind: HistoryFactKind,
) -> None:
    harness = Harness()
    if target_kind is not HistoryFactKind.RECORDING:
        harness.resident.synchronous_outcome = completed_capture
    if target_kind in {
        HistoryFactKind.WAITING_ANALYSIS,
        HistoryFactKind.ANALYZING,
    }:
        harness.save_dispatcher.synchronous_outcome = saved_outcome
    clock_entered = Event()
    clock_release = Event()
    harness.clock.hook.entered = clock_entered
    harness.clock.hook.release = clock_release
    coordinator = harness.coordinator()
    original_publish_active = coordinator._publish_active
    terminal_threads = []
    injected = False

    def publish_active(
        context,
        kind,
        projection_phase,
        action_kind,
        expected_phase,
    ):
        nonlocal injected
        if kind is target_kind and not injected:
            injected = True
            terminal_thread = Thread(
                target=lambda: coordinator._terminalize(
                    context,
                    TerminalConclusion.ERROR,
                    {
                        HistoryFactKind.RECORDING: (
                            TerminalErrorCategory.CAPTURE_FAILED
                        ),
                        HistoryFactKind.WAITING_SAVE: (
                            TerminalErrorCategory.AUDIO_SAVE_FAILED
                        ),
                        HistoryFactKind.WAITING_ANALYSIS: (
                            TerminalErrorCategory.ANALYSIS_FAILED
                        ),
                        HistoryFactKind.ANALYZING: (
                            TerminalErrorCategory.ANALYSIS_FAILED
                        ),
                    }[target_kind],
                    f"terminal in pre-{target_kind.name} handoff",
                )
            )
            terminal_threads.append(terminal_thread)
            terminal_thread.start()
            assert clock_entered.wait(2)
        try:
            return original_publish_active(
                context,
                kind,
                projection_phase,
                action_kind,
                expected_phase,
            )
        finally:
            if kind is target_kind:
                clock_release.set()
                for terminal_thread in terminal_threads:
                    terminal_thread.join(2)

    coordinator._publish_active = publish_active

    result = coordinator.start_captured(captured_request())

    assert injected
    assert all(not thread.is_alive() for thread in terminal_threads)
    assert result.status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.persistence.terminal_calls) == 1
    assert not [
        event for event in harness.history.calls if event.kind is target_kind
    ]
    assert coordinator.snapshot().accepted_active_run_ids == ()
    assert coordinator._attempts == {}
    assert coordinator._save_jobs == {}


_DOMAIN_CONTINUATION_AFTER_HISTORY = {
    HistoryFactKind.RECORDING: "start",
    HistoryFactKind.WAITING_SAVE: "save_submit",
    HistoryFactKind.WAITING_ANALYSIS: "scheduler_submit",
}


def _configure_active_history_target(
    harness: Harness,
    target_kind: HistoryFactKind,
) -> None:
    if target_kind is not HistoryFactKind.RECORDING:
        harness.resident.synchronous_outcome = completed_capture
    if target_kind is HistoryFactKind.WAITING_ANALYSIS:
        harness.save_dispatcher.synchronous_outcome = saved_outcome


def _fail_only_target_projector(
    harness: Harness,
    target_kind: HistoryFactKind,
) -> None:
    original_project = harness.projector.project

    def project(fact):
        if fact.kind is not target_kind:
            return original_project(fact)
        harness.projector.exception = RuntimeError(
            f"{target_kind.name} projector failed"
        )
        try:
            return original_project(fact)
        finally:
            harness.projector.exception = None

    harness.projector.project = project


def _fail_only_target_sink(
    harness: Harness,
    target_kind: HistoryFactKind,
) -> None:
    original_publish = harness.history.publish

    def publish(event):
        if event.kind is not target_kind:
            return original_publish(event)
        harness.history.exception = RuntimeError(
            f"{target_kind.name} sink failed " + ("x" * 600)
        )
        try:
            return original_publish(event)
        finally:
            harness.history.exception = None

    harness.history.publish = publish


@pytest.mark.parametrize(
    "target_kind",
    tuple(
        kind
        for kind in _DOMAIN_CONTINUATION_AFTER_HISTORY
        if kind is not HistoryFactKind.WAITING_ANALYSIS
    ),
)
@pytest.mark.parametrize(
    "failure",
    [
        "projector_exception",
        "sink_exception",
    ],
)
def test_active_history_failure_is_diagnostic_and_continues_domain_pipeline(
    target_kind: HistoryFactKind,
    failure,
) -> None:
    harness = Harness()
    _configure_active_history_target(harness, target_kind)
    if failure == "projector_exception":
        _fail_only_target_projector(harness, target_kind)
        diagnostic_stage = "active history projector"
    else:
        _fail_only_target_sink(harness, target_kind)
        diagnostic_stage = "active history sink"
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert _DOMAIN_CONTINUATION_AFTER_HISTORY[target_kind] in harness.trace
    assert len(
        [fact for fact in harness.projector.calls if fact.kind is target_kind]
    ) == 1
    assert len(
        [
            event
            for event in harness.history.calls
            if event.kind is target_kind
        ]
    ) == (0 if failure == "projector_exception" else 1)
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == diagnostic_stage
    ]
    assert len(diagnostics) == 1
    assert (
        diagnostics[0].category
        is TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED
    )
    assert len(diagnostics[0].message.encode("utf-8")) <= 512
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        assert context.history_allowed


@pytest.mark.parametrize(
    "target_kind",
    tuple(_DOMAIN_CONTINUATION_AFTER_HISTORY),
)
@pytest.mark.parametrize(
    "boundary",
    ["projector", "sink_exception"],
)
def test_active_history_failure_contender_wins_before_later_domain_port(
    target_kind: HistoryFactKind,
    boundary: str,
) -> None:
    harness = Harness()
    _configure_active_history_target(harness, target_kind)
    if boundary == "projector":
        _fail_only_target_projector(harness, target_kind)
        hook = harness.projector.hooks[target_kind]
        diagnostic_stage = "active history projector"
    else:
        _fail_only_target_sink(harness, target_kind)
        hook = harness.history.hooks[target_kind]
        diagnostic_stage = "active history sink"
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    results = []
    thread = Thread(
        target=lambda: results.append(
            coordinator.start_captured(captured_request())
        )
    )

    thread.start()
    assert entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        {
            HistoryFactKind.RECORDING: TerminalErrorCategory.CAPTURE_FAILED,
            HistoryFactKind.WAITING_SAVE: (
                TerminalErrorCategory.AUDIO_SAVE_FAILED
            ),
            HistoryFactKind.WAITING_ANALYSIS: (
                TerminalErrorCategory.ANALYSIS_FAILED
            ),
        }[target_kind],
        f"terminal during {target_kind.name} {boundary}",
    )

    release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert _DOMAIN_CONTINUATION_AFTER_HISTORY[target_kind] not in harness.trace
    assert len(harness.persistence.terminal_calls) == 1
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == diagnostic_stage
    ]
    assert len(diagnostics) == 1


_ATOMIC_TERMINAL_HANDOFF_STAGES = (
    "plc_mark",
    *(
        stage
        for stage in _POST_ACCEPT_CONTENDER_STAGES
        if stage != "terminal_commit"
    ),
)


@pytest.mark.parametrize("stage", _ATOMIC_TERMINAL_HANDOFF_STAGES)
def test_first_terminal_handoff_claims_before_second_contender(
    stage: str,
) -> None:
    harness = Harness()
    if stage != "plc_mark":
        _configure_post_accept_contender_stage(harness, stage)
    hook = (
        harness.plc.hooks["mark"]
        if stage == "plc_mark"
        else _post_accept_stage_hook(harness, stage)
    )
    action_entered = Event()
    action_release = Event()
    hook.entered = action_entered
    hook.release = action_release
    coordinator = harness.coordinator()
    handoff_entered = Event()
    handoff_release = Event()
    original_terminalize_contender = coordinator._terminalize_contender

    def terminalize_contender(context, contender):
        handoff_entered.set()
        assert handoff_release.wait(2)
        return original_terminalize_contender(context, contender)

    coordinator._terminalize_contender = terminalize_contender
    result_box = []
    request = captured_request(
        source=TriggerSource.PLC
        if stage == "plc_mark"
        else TriggerSource.MANUAL
    )
    if stage == "imported_active_commit":
        start = lambda: coordinator.start_imported(imported_request())
    else:
        start = lambda: coordinator.start_captured(request)
    thread = Thread(target=lambda: result_box.append(start()))

    thread.start()
    assert action_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    first_reason = f"FIRST terminal during {stage}"
    second_reason = f"SECOND terminal during {stage}"
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        (
            TerminalErrorCategory.AUDIO_SAVE_FAILED
            if stage == "imported_active_commit"
            else (
                TerminalErrorCategory.ANALYSIS_FAILED
                if context.attempt_id is not None
                else TerminalErrorCategory.CAPTURE_FAILED
            )
        ),
        first_reason,
    )

    action_release.set()
    assert handoff_entered.wait(2)
    with coordinator._lock:
        claim_state = (
            context.terminal_claimed,
            context.reservation_cancel_attempted,
            context.save_cancel_attempted,
            context.scheduler_cancel_attempted,
        )
    second_result = coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
        second_reason,
    )
    handoff_release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert claim_state[0]
    assert second_result.status is StartStatus.ACCEPTED_ACTIVE
    assert len(harness.persistence.terminal_calls) == 1
    terminal = harness.persistence.terminal_calls[0]
    assert terminal.reason == first_reason
    assert terminal.reason != second_reason
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    reservation_stages = {
        "plc_mark",
        "captured_active_commit",
        "capture_register",
        "recording_projector",
        "recording_sink",
        "capture_start",
    }
    save_stages = {"save_submit"}
    scheduler_stages = {
        "scheduler_submit",
        "analyzing_projector",
        "analyzing_sink",
    }
    assert claim_state[1] is (stage in reservation_stages)
    assert claim_state[2] is (stage in save_stages)
    assert claim_state[3] is (stage in scheduler_stages)
    assert len(harness.resident.cancel_calls) == (
        1 if stage in reservation_stages else 0
    )
    assert len(harness.save_dispatcher.cancel_calls) == (
        1 if stage in save_stages else 0
    )
    assert len(harness.scheduler.cancel_calls) == (
        1 if stage in scheduler_stages else 0
    )
    if stage in scheduler_stages:
        assert harness.scheduler.cancel_calls[0][2] == first_reason
    if stage == "plc_mark":
        assert len(harness.plc.publish_calls) == 1
        assert harness.plc.publish_calls[0][1].reason == first_reason


@pytest.mark.parametrize(
    "target_kind",
    tuple(_DOMAIN_CONTINUATION_AFTER_HISTORY),
)
def test_first_terminal_handoff_is_claimed_before_sink_exception_diagnostic(
    target_kind: HistoryFactKind,
) -> None:
    harness = Harness()
    _configure_active_history_target(harness, target_kind)
    _fail_only_target_sink(harness, target_kind)
    sink_entered = Event()
    sink_release = Event()
    harness.history.hooks[target_kind].entered = sink_entered
    harness.history.hooks[target_kind].release = sink_release
    diagnostic_entered = Event()
    diagnostic_release = Event()
    original_record = harness.diagnostics.record

    def record(fact):
        diagnostic_entered.set()
        assert diagnostic_release.wait(2)
        original_record(fact)

    harness.diagnostics.record = record
    coordinator = harness.coordinator()
    result_box = []
    thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_captured(captured_request())
        )
    )

    thread.start()
    assert sink_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
    first_reason = f"FIRST terminal during {target_kind.name} sink"
    second_reason = f"SECOND terminal during {target_kind.name} diagnostic"
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.CAPTURE_FAILED,
        first_reason,
    )

    sink_release.set()
    assert diagnostic_entered.wait(2)
    with coordinator._lock:
        claim_state = (
            context.terminal_claimed,
            context.reservation_cancel_attempted,
        )
    second_result = coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
        second_reason,
    )
    diagnostic_release.set()
    thread.join(2)

    assert not thread.is_alive()
    assert claim_state == (
        True,
        target_kind is HistoryFactKind.RECORDING,
    )
    assert second_result.status is StartStatus.ACCEPTED_ACTIVE
    assert len(harness.persistence.terminal_calls) == 1


    terminal = harness.persistence.terminal_calls[0]
    assert terminal.reason == first_reason
    assert terminal.reason != second_reason
    assert result_box[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.resident.cancel_calls) == (
        1 if target_kind is HistoryFactKind.RECORDING else 0
    )
    assert _DOMAIN_CONTINUATION_AFTER_HISTORY[target_kind] not in harness.trace
    assert len(
        [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "active history sink"
        ]
    ) == 1


@pytest.mark.parametrize(
    "target_kind",
    [
        HistoryFactKind.RECORDING,
        HistoryFactKind.WAITING_SAVE,
        HistoryFactKind.WAITING_ANALYSIS,
        HistoryFactKind.ANALYZING,
    ],
)
@pytest.mark.parametrize("raises", [False, True])
def test_task3b_active_history_is_one_unlocked_attempt_with_causal_cursor(
    target_kind: HistoryFactKind,
    raises: bool,
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    coordinator = harness.coordinator()
    original_publish = harness.history.publish

    def one_way_publish(event):
        assert not coordinator._lock._is_owned()
        original_publish(event)
        if raises and event.kind is target_kind:
            raise RuntimeError(f"{target_kind.name} dispatch failed")
        return object()

    harness.history.publish = one_way_publish

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    expected_kinds = [
        HistoryFactKind.RECORDING,
        HistoryFactKind.WAITING_SAVE,
        HistoryFactKind.WAITING_ANALYSIS,
        HistoryFactKind.ANALYZING,
    ]
    assert [event.kind for event in harness.history.calls] == expected_kinds
    assert [event.expected_revision for event in harness.history.calls] == [
        package.ABSENT,
        1,
        2,
        3,
    ]
    assert [event.proposed_revision for event in harness.history.calls] == [
        1,
        2,
        3,
        4,
    ]
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        assert context.last_published_revision == 4
        assert context.history_allowed
    assert len(
        [
            event
            for event in harness.history.calls
            if event.kind is target_kind
        ]
    ) == 1
    assert ("bridge_submit_initial" in harness.trace)
    assert len(harness.persistence.active_calls) == 1
    assert len(harness.save_dispatcher.calls) == 1
    if raises:
        assert len(
            [
                fact
                for fact in harness.diagnostics.calls
                if fact.stage == "active history sink"
            ]
        ) == 1


_INVALID_PROJECTED_EVENT_CASES = (
    "structural-object",
    "malformed-exact-type",
    "cross-run",
    "kind",
    "operation",
    "projection",
    "expected-revision",
    "causal-successor",
)


def _invalid_active_projected_event(event, defect):
    if defect == "structural-object":
        return SimpleNamespace(
            kind=event.kind,
            run_id=event.run_id,
            expected_revision=event.expected_revision,
            operation_id=event.operation_id,
            proposed_revision=event.proposed_revision,
            projection=event.projection,
        )
    if defect == "malformed-exact-type":
        object.__setattr__(event, "run_id", object())
    elif defect == "cross-run":
        object.__setattr__(event, "run_id", RUN_IDS[1])
    elif defect == "kind":
        object.__setattr__(event, "kind", HistoryFactKind.WAITING_SAVE)
    elif defect == "operation":
        object.__setattr__(event, "operation_id", ATTEMPT_IDS[1])
    elif defect == "projection":
        object.__setattr__(
            event,
            "projection",
            replace(
                event.projection,
                phase=package.ResultProjectionPhase.WAITING_SAVE,
            ),
        )
    elif defect == "expected-revision":
        object.__setattr__(event, "expected_revision", 1)
        object.__setattr__(event, "proposed_revision", 2)
    else:
        assert defect == "causal-successor"
        object.__setattr__(event, "proposed_revision", 2)
    return event


@pytest.mark.parametrize("defect", _INVALID_PROJECTED_EVENT_CASES)
def test_task3b_active_projector_output_is_validated_before_dispatch(
    defect: str,
) -> None:
    harness = Harness()
    harness.resident.synchronous_outcome = completed_capture
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    original_project = harness.projector.project

    def project(fact):
        event = original_project(fact)
        if fact.kind is HistoryFactKind.RECORDING:
            return _invalid_active_projected_event(event, defect)
        return event

    harness.projector.project = project
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())

    assert result.status is StartStatus.ACCEPTED_ACTIVE
    assert "start" in harness.trace
    assert "save_submit" in harness.trace
    assert "bridge_submit_initial" in harness.trace
    assert [
        event.kind for event in harness.history.calls
    ] == [
        HistoryFactKind.WAITING_SAVE,
        HistoryFactKind.WAITING_ANALYSIS,
        HistoryFactKind.ANALYZING,
    ]
    assert [event.expected_revision for event in harness.history.calls] == [
        package.ABSENT,
        1,
        2,
    ]
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        assert context.last_published_revision == 3
        assert context.history_allowed
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "active history projector"
    ]
    assert len(diagnostics) == 1
    assert len(diagnostics[0].message.encode("utf-8")) <= 512


# These pre-composite tests exercised Coordinator-owned attempt creation,
# raw Scheduler receipts/outcomes, and post-acquisition terminal persistence.
# Task 3 replaces those contracts with the one-shot bridge report tests in
# test_bridge_initial.py; keeping them collectible would assert forbidden
# duplicate authority.
for _obsolete_test_name in (
    "test_save_callback_before_receipt_is_held_until_receipt_reconcile",
    "test_duplicate_save_job_id_does_not_overwrite_provisional_first_owner",
    "test_duplicate_attempt_id_does_not_overwrite_provisional_first_owner",
    "test_scheduler_queued_commit_and_waiting_history_precede_submit",
    "test_scheduler_receives_only_lightweight_audio_and_frozen_input",
    "test_scheduler_early_outcome_waits_for_analyzing_history",
    "test_scheduler_rejected_receipt_terminalizes_once",
    "test_scheduler_submit_exception_terminalizes_once",
    "test_scheduler_rejected_receipt_diagnoses_contradicted_early_outcome",
    "test_scheduler_mismatched_receipt_terminalizes_as_invalid",
    "test_scheduler_malformed_accepted_receipt_cancels_submitted_child_first",
    "test_scheduler_mismatched_then_exact_outcome_terminalizes_once",
    "test_terminal_commit_precedes_history_and_plc",
    "test_terminal_commit_failure_skips_history_and_settles_plc_once",
    "test_terminal_history_exception_does_not_suppress_plc",
    "test_terminal_plc_exception_does_not_roll_back_history",
    "test_terminal_publishing_stays_visible_until_plc_call_returns",
    "test_terminal_publishing_replaces_active_context_with_fixed_state",
    "test_ignored_analysis_callbacks_diagnose_once_outside_lock",
    "test_callback_exception_handler_reads_indexes_only_under_lock",
    "test_post_accept_action_matrix_consumes_contender_before_next_port",
    "test_blocked_terminal_clock_makes_analysis_outcome_stale_before_commit",
    "test_normal_terminal_and_preaccept_never_use_cleanup_sentinel",
    "test_terminal_history_boundary_is_unlocked_and_precedes_plc",
    "test_first_terminal_handoff_claims_before_second_contender",
):
    globals()[_obsolete_test_name].__test__ = False
