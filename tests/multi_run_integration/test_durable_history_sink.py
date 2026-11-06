from __future__ import annotations

from dataclasses import replace
from inspect import signature
from types import SimpleNamespace

import pytest

from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    HistoryFactKind,
    ResultHistoryEventSink,
    TerminalConclusion,
    TerminalErrorCategory,
)
from app.service.multi_run_integration.history_qt_adapter import (
    HistoryQtAdapter,
    HistoryQtAdapterDiagnosticCode,
)
from app.service.result_history import ResultHistoryStore
from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
)
from tests.multi_run_integration.fakes import (
    RUN_IDS,
    history_labels,
    projected_event,
)


class _RepositoryProbe:
    def __init__(self, *, revision: int | None = 0, trace=None):
        self.revision = revision
        self.calls = []
        self.trace = trace

    def advance_causal_revision(
        self,
        run_id,
        *,
        expected_revision,
        proposed_revision,
    ):
        call = (run_id, expected_revision, proposed_revision)
        self.calls.append(call)
        if self.trace is not None:
            self.trace.append(("cas", call))
        durable_expected = 0 if expected_revision is ABSENT else expected_revision
        if self.revision is None or self.revision != durable_expected:
            return TransactionResult(
                state=TransactionState.ROLLED_BACK,
                reason="CAUSAL_REVISION_COMPARE_AND_SWAP_REJECTED",
            )
        self.revision = proposed_revision
        return TransactionResult(
            state=TransactionState.COMMITTED,
            value=True,
        )


class _ForwardProbe:
    def __init__(self, *, trace=None, error: Exception | None = None):
        self.calls = []
        self.trace = trace
        self.error = error

    def publish(self, event):
        self.calls.append(event)
        if self.trace is not None:
            self.trace.append(("forward", event))
        if self.error is not None:
            raise self.error


class _DashboardProbe:
    def __init__(self):
        self.store = ResultHistoryStore()
        self.calls = []
        self.raise_on_apply = False
        self._thread = object()

    def thread(self):
        return self._thread

    def apply_history_event(self, event):
        self.calls.append(event)
        if self.raise_on_apply:
            raise RuntimeError("store application failed")
        return self.store.apply(event)


def _sink(repository, downstream, diagnostics=None):
    from app.service.multi_run_integration.durable_history_sink import (
        DurableHistorySink,
    )

    return DurableHistorySink(
        repository,
        downstream,
        diagnostic_callback=(
            None if diagnostics is None else diagnostics.append
        ),
    )


def _accepted_event(
    *,
    run_id=RUN_IDS[0],
    expected_revision=ABSENT,
    proposed_revision=1,
    kind=HistoryFactKind.RECORDING,
):
    return projected_event(
        kind,
        run_id=run_id,
        expected_revision=expected_revision,
        proposed_revision=proposed_revision,
    )


def _preaccept_event(category, conclusion):
    return projected_event(
        HistoryFactKind.RUN_TERMINATED,
        conclusion=conclusion,
        error_category=category,
        reason=category.name,
        labels=history_labels(config_id=None, fault_types=()),
    )


def test_history_sink_persists_exact_event_cas_before_forwarding():
    trace = []
    repository = _RepositoryProbe(trace=trace)
    downstream = _ForwardProbe(trace=trace)
    event = _accepted_event()

    _sink(repository, downstream).publish(event)

    assert trace == [
        (("cas"), (event.run_id, ABSENT, 1)),
        ("forward", event),
    ]
    assert downstream.calls == [event]


def test_history_sink_sequence_advances_zero_through_active_terminal_manual_reanalysis():
    repository = _RepositoryProbe()
    downstream = _ForwardProbe()
    sink = _sink(repository, downstream)
    operation_id = RUN_IDS[8]
    events = [
        _accepted_event(),
        _accepted_event(
            kind=HistoryFactKind.WAITING_SAVE,
            expected_revision=1,
            proposed_revision=2,
        ),
        _accepted_event(
            kind=HistoryFactKind.WAITING_ANALYSIS,
            expected_revision=2,
            proposed_revision=3,
        ),
        projected_event(
            HistoryFactKind.ANALYZING,
            expected_revision=3,
            proposed_revision=4,
            operation_id=operation_id,
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            expected_revision=4,
            proposed_revision=5,
            operation_id=operation_id,
            conclusion=TerminalConclusion.ERROR,
            error_category=TerminalErrorCategory.ANALYSIS_FAILED,
            reason="analysis failed",
        ),
        projected_event(
            HistoryFactKind.TERMINAL_RESULT_REPLACED,
            expected_revision=5,
            proposed_revision=6,
            conclusion=TerminalConclusion.OK,
            manual_revision=6,
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_STARTED,
            expected_revision=6,
            proposed_revision=7,
            operation_id=operation_id,
            conclusion=TerminalConclusion.OK,
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_CANCELLED,
            expected_revision=7,
            proposed_revision=8,
            operation_id=operation_id,
            conclusion=TerminalConclusion.OK,
        ),
    ]

    for event in events:
        sink.publish(event)

    assert repository.revision == 8
    assert [call[2] for call in repository.calls] == list(range(1, 9))
    assert downstream.calls == events


def test_history_sink_cas_mismatch_forwards_nothing_and_fails_closed_once():
    from app.service.multi_run_integration.durable_history_sink import (
        DurableHistoryPersistenceError,
    )

    repository = _RepositoryProbe(revision=2)
    downstream = _ForwardProbe()
    diagnostics = []
    event = _accepted_event(
        kind=HistoryFactKind.WAITING_SAVE,
        expected_revision=1,
        proposed_revision=2,
    )

    with pytest.raises(
        DurableHistoryPersistenceError,
        match="CAUSAL_REVISION_COMPARE_AND_SWAP_REJECTED",
    ):
        _sink(repository, downstream, diagnostics).publish(event)

    assert repository.calls == [(event.run_id, 1, 2)]
    assert downstream.calls == []
    assert len(diagnostics) == 1
    assert diagnostics[0].run_id == event.run_id
    assert len(diagnostics[0].detail) <= 240


def test_exact_five_preaccept_events_bypass_cas_and_remain_operation_ineligible():
    combinations = (
        (
            TerminalErrorCategory.PREFLIGHT_REJECTED,
            TerminalConclusion.ERROR,
        ),
        (
            TerminalErrorCategory.INVALID_FROZEN_INPUT,
            TerminalConclusion.ERROR,
        ),
        (
            TerminalErrorCategory.CAPTURE_CAPACITY,
            TerminalConclusion.CAPACITY_REJECTED,
        ),
        (
            TerminalErrorCategory.RESIDENT_UNAVAILABLE,
            TerminalConclusion.ERROR,
        ),
        (
            TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
            TerminalConclusion.ERROR,
        ),
    )
    repository = _RepositoryProbe(revision=None)
    downstream = _ForwardProbe()
    sink = _sink(repository, downstream)
    events = [_preaccept_event(*combination) for combination in combinations]

    for event in events:
        sink.publish(event)

    assert repository.calls == []
    assert downstream.calls == events
    assert all(event.operation_id is None for event in events)
    assert all(event.expected_revision is ABSENT for event in events)


def test_missing_row_or_absent_non_preaccept_event_fails_closed_without_enqueue():
    from app.service.multi_run_integration.durable_history_sink import (
        DurableHistoryPersistenceError,
    )

    repository = _RepositoryProbe(revision=None)
    downstream = _ForwardProbe()
    diagnostics = []
    sink = _sink(repository, downstream, diagnostics)
    events = (
        _accepted_event(),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            conclusion=TerminalConclusion.ERROR,
            error_category=TerminalErrorCategory.ANALYSIS_FAILED,
            reason="not a protected preaccept category",
        ),
    )

    for event in events:
        with pytest.raises(DurableHistoryPersistenceError):
            sink.publish(event)

    assert len(repository.calls) == 2
    assert downstream.calls == []
    assert len(diagnostics) == 2


def test_dashboard_revision_equals_durable_only_after_successful_store_application():
    repository = _RepositoryProbe()
    dashboard = _DashboardProbe()
    adapter = HistoryQtAdapter(dashboard)
    queued = []
    adapter._enqueue = queued.append
    event = _accepted_event()

    _sink(repository, adapter).publish(event)

    assert repository.revision == 1
    assert dashboard.store.snapshot().active_cards == ()
    assert len(queued) == 1

    dashboard.apply_history_event(queued[0])

    card = dashboard.store.snapshot().active_cards[0]
    assert card.revision == repository.revision == event.proposed_revision


def test_mapping_failure_keeps_durable_revision_without_retry_or_false_success():
    repository = _RepositoryProbe()
    dashboard = _DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=diagnostics.append,
    )
    event = _accepted_event()
    event = replace(
        event,
        projection=replace(
            event.projection,
            history_labels=replace(
                event.projection.history_labels,
                canonical_bytes=b"{}",
            ),
        ),
    )

    _sink(repository, adapter).publish(event)

    assert repository.revision == 1
    assert len(repository.calls) == 1
    assert dashboard.calls == []
    assert [item.code for item in diagnostics] == [
        HistoryQtAdapterDiagnosticCode.MAPPING_FAILED
    ]


def test_enqueue_failure_keeps_durable_revision_without_retry_or_false_success():
    repository = _RepositoryProbe()
    dashboard = _DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=diagnostics.append,
    )
    adapter._enqueue = lambda _event: (_ for _ in ()).throw(
        RuntimeError("queue failed")
    )

    _sink(repository, adapter).publish(_accepted_event())

    assert repository.revision == 1
    assert len(repository.calls) == 1
    assert dashboard.calls == []
    assert [item.code for item in diagnostics] == [
        HistoryQtAdapterDiagnosticCode.QUEUE_FAILED
    ]


def test_target_destruction_keeps_durable_revision_without_retry_or_false_success():
    repository = _RepositoryProbe()
    dashboard = _DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=diagnostics.append,
    )
    queued = []
    adapter._enqueue = queued.append

    _sink(repository, adapter).publish(_accepted_event())
    adapter._target_ref = lambda: None
    adapter._deliver(queued[0])

    assert repository.revision == 1
    assert len(repository.calls) == 1
    assert dashboard.calls == []
    assert [item.code for item in diagnostics] == [
        HistoryQtAdapterDiagnosticCode.TARGET_DESTROYED
    ]


def test_wrong_thread_delivery_keeps_durable_revision_without_retry_or_false_success(
    monkeypatch,
):
    import app.service.multi_run_integration.history_qt_adapter as adapter_module

    repository = _RepositoryProbe()
    dashboard = _DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=diagnostics.append,
    )
    queued = []
    adapter._enqueue = queued.append

    _sink(repository, adapter).publish(_accepted_event())
    monkeypatch.setattr(
        adapter_module,
        "QThread",
        SimpleNamespace(currentThread=lambda: object()),
    )
    adapter._deliver(queued[0])

    assert repository.revision == 1
    assert len(repository.calls) == 1
    assert dashboard.calls == []
    assert [item.code for item in diagnostics] == [
        HistoryQtAdapterDiagnosticCode.WRONG_QT_THREAD
    ]


def test_store_apply_failure_keeps_durable_revision_and_reports_degraded_once(
    monkeypatch,
):
    import app.service.multi_run_integration.history_qt_adapter as adapter_module

    repository = _RepositoryProbe()
    dashboard = _DashboardProbe()
    dashboard.raise_on_apply = True
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=diagnostics.append,
    )
    queued = []
    adapter._enqueue = queued.append

    _sink(repository, adapter).publish(_accepted_event())
    monkeypatch.setattr(
        adapter_module,
        "QThread",
        SimpleNamespace(currentThread=dashboard.thread),
    )
    adapter._deliver(queued[0])

    assert repository.revision == 1
    assert len(repository.calls) == 1
    assert len(dashboard.calls) == 1
    assert [item.code for item in diagnostics] == [
        HistoryQtAdapterDiagnosticCode.APPLY_FAILED
    ]


@pytest.mark.parametrize(
    "failure",
    ("mapping", "enqueue", "target", "wrong_thread", "apply"),
)
def test_each_history_failure_reports_degraded_once_and_rejects_stale_or_absent_operation(
    failure,
    monkeypatch,
):
    import app.service.multi_run_integration.history_qt_adapter as adapter_module
    from app.service.multi_run_integration.durable_history_sink import (
        DurableHistoryPersistenceError,
    )

    repository = _RepositoryProbe(revision=1)
    dashboard = _DashboardProbe()
    adapter_diagnostics = []
    persistence_diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=adapter_diagnostics.append,
    )
    queued = []
    adapter._enqueue = queued.append
    event = _accepted_event(
        kind=HistoryFactKind.WAITING_SAVE,
        expected_revision=1,
        proposed_revision=2,
    )
    if failure == "mapping":
        event = replace(
            event,
            projection=replace(
                event.projection,
                history_labels=replace(
                    event.projection.history_labels,
                    canonical_bytes=b"{}",
                ),
            ),
        )
    elif failure == "enqueue":
        adapter._enqueue = lambda _event: (_ for _ in ()).throw(
            RuntimeError("queue failed")
        )
    elif failure == "apply":
        dashboard.raise_on_apply = True

    sink = _sink(repository, adapter, persistence_diagnostics)
    sink.publish(event)
    if failure == "target":
        adapter._target_ref = lambda: None
        adapter._deliver(queued[0])
    elif failure == "wrong_thread":
        monkeypatch.setattr(
            adapter_module,
            "QThread",
            SimpleNamespace(currentThread=lambda: object()),
        )
        adapter._deliver(queued[0])
    elif failure == "apply":
        monkeypatch.setattr(
            adapter_module,
            "QThread",
            SimpleNamespace(currentThread=dashboard.thread),
        )
        adapter._deliver(queued[0])

    stale = _accepted_event(
        kind=HistoryFactKind.WAITING_SAVE,
        expected_revision=1,
        proposed_revision=2,
    )
    absent = _accepted_event()
    for operation in (stale, absent):
        with pytest.raises(DurableHistoryPersistenceError):
            sink.publish(operation)

    assert repository.revision == 2
    assert len(adapter_diagnostics) == 1
    assert len(persistence_diagnostics) == 2
    assert all(len(item.detail) <= 240 for item in adapter_diagnostics)
    assert all(len(item.detail) <= 240 for item in persistence_diagnostics)


def test_existing_coordinator_contract_and_call_order_need_no_edit():
    trace = []
    sink = _sink(
        _RepositoryProbe(trace=trace),
        _ForwardProbe(trace=trace),
    )
    protocol_signature = signature(ResultHistoryEventSink.publish)
    sink_signature = signature(sink.publish)
    event = _accepted_event()

    sink.publish(event)

    assert tuple(protocol_signature.parameters) == ("self", "event")
    assert tuple(sink_signature.parameters) == ("event",)
    assert [item[0] for item in trace] == ["cas", "forward"]
