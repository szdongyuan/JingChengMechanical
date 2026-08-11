from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from threading import Thread
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    FrozenPayload,
    HistoryFactKind,
    TerminalConclusion,
    TerminalErrorCategory,
)
from app.service.result_history import (
    ActiveRunChanged,
    ReanalysisCancelled,
    ReanalysisCompleted,
    ReanalysisStarted,
    ResultConclusion,
    ResultHistoryStore,
    RunTerminated,
    TerminalResultReplaced,
)
from app.service.result_history_contracts import (
    HistoryProjectionDiagnosticCode,
    ResultOperationRejected,
    ResultOperationRejectionCode,
)
from ui.modules.result_dashboard import ResultDashboardModule
from tests.multi_run_integration.fakes import (
    RUN_IDS,
    bridge_result,
    history_labels,
    projected_event,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class DashboardProbe:
    def __init__(self):
        self.store = ResultHistoryStore()
        self.calls = []
        self.threads = []
        self.raise_on_apply = False

    def thread(self):
        return QThread.currentThread()

    def apply_history_event(self, event):
        self.calls.append(event)
        self.threads.append(QThread.currentThread())
        if self.raise_on_apply:
            raise RuntimeError("store/projection failed")
        return self.store.apply(event)


def _drain(qapp, attempts=10):
    for _ in range(attempts):
        qapp.processEvents()


def test_every_history_fact_maps_exhaustively_in_fifo_order(qapp):
    from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter

    dashboard = DashboardProbe()
    evictions = []
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        eviction_callback=evictions.append,
        diagnostic_callback=diagnostics.append,
    )
    operation_id = RUN_IDS[8]
    events = []
    for index, kind in enumerate(
        (
            HistoryFactKind.RECORDING,
            HistoryFactKind.WAITING_SAVE,
            HistoryFactKind.WAITING_ANALYSIS,
            HistoryFactKind.ANALYZING,
        )
    ):
        events.append(
            projected_event(
                kind,
                expected_revision=(index if index else ABSENT),
                proposed_revision=index + 1,
                operation_id=(
                    RUN_IDS[7]
                    if kind is HistoryFactKind.ANALYZING
                    else None
                ),
            )
        )
    events.extend(
        (
            projected_event(
                HistoryFactKind.RUN_TERMINATED,
                expected_revision=4,
                proposed_revision=5,
                operation_id=RUN_IDS[7],
                result_attempt_id=RUN_IDS[7],
            ),
            projected_event(
                HistoryFactKind.REANALYSIS_STARTED,
                expected_revision=5,
                proposed_revision=6,
                operation_id=operation_id,
                result_attempt_id=RUN_IDS[7],
            ),
            projected_event(
                HistoryFactKind.REANALYSIS_CANCELLED,
                expected_revision=6,
                proposed_revision=7,
                operation_id=operation_id,
                result_attempt_id=RUN_IDS[7],
            ),
            projected_event(
                HistoryFactKind.REANALYSIS_STARTED,
                expected_revision=7,
                proposed_revision=8,
                operation_id=operation_id,
                result_attempt_id=RUN_IDS[7],
            ),
            projected_event(
                HistoryFactKind.REANALYSIS_COMPLETED,
                expected_revision=8,
                proposed_revision=9,
                operation_id=operation_id,
                conclusion=TerminalConclusion.NG,
                result_attempt_id=operation_id,
            ),
            projected_event(
                HistoryFactKind.TERMINAL_RESULT_REPLACED,
                expected_revision=9,
                proposed_revision=10,
                conclusion=TerminalConclusion.NG,
                fault_type="异响",
                manual_revision=10,
                result_attempt_id=operation_id,
            ),
            projected_event(
                HistoryFactKind.REANALYSIS_STARTED,
                expected_revision=10,
                proposed_revision=11,
                operation_id=operation_id,
                conclusion=TerminalConclusion.NG,
                fault_type="异响",
                manual_revision=10,
                result_attempt_id=operation_id,
            ),
            projected_event(
                HistoryFactKind.REANALYSIS_CANCELLED,
                expected_revision=11,
                proposed_revision=12,
                operation_id=operation_id,
                conclusion=TerminalConclusion.NG,
                fault_type="异响",
                manual_revision=10,
                result_attempt_id=operation_id,
            ),
        )
    )

    def publish_from_worker():
        for event in events:
            adapter.publish(event)

    publisher = Thread(target=publish_from_worker)
    publisher.start()
    publisher.join(timeout=2)
    assert not publisher.is_alive()
    _drain(qapp)

    assert [type(item) for item in dashboard.calls] == [
        ActiveRunChanged,
        ActiveRunChanged,
        ActiveRunChanged,
        ActiveRunChanged,
        RunTerminated,
        ReanalysisStarted,
        ReanalysisCancelled,
        ReanalysisStarted,
        ReanalysisCompleted,
        TerminalResultReplaced,
        ReanalysisStarted,
        ReanalysisCancelled,
    ]
    assert all(thread is QThread.currentThread() for thread in dashboard.threads)
    card = dashboard.store.snapshot().terminal_cards[0]
    assert card.config.fault_types == ("异响", "漏装")
    assert card.fault_type == "异响"
    assert not diagnostics
    assert not evictions


def test_one_run_keeps_its_first_start_and_terminal_times_across_phase_updates(
    qapp,
):
    from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter

    dashboard = DashboardProbe()
    times = iter(
        (
            datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 30, 8, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 30, 8, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 7, 30, 8, 0, 3, tzinfo=timezone.utc),
        )
    )
    adapter = HistoryQtAdapter(dashboard, clock=lambda: next(times))
    events = (
        projected_event(
            HistoryFactKind.RECORDING,
            expected_revision=ABSENT,
            proposed_revision=1,
        ),
        projected_event(
            HistoryFactKind.WAITING_SAVE,
            expected_revision=1,
            proposed_revision=2,
        ),
        projected_event(
            HistoryFactKind.ANALYZING,
            expected_revision=2,
            proposed_revision=3,
            operation_id=RUN_IDS[7],
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            expected_revision=3,
            proposed_revision=4,
            operation_id=RUN_IDS[7],
            result_attempt_id=RUN_IDS[7],
        ),
    )

    for event in events:
        adapter.publish(event)
    _drain(qapp)

    cards = [event.card for event in dashboard.calls]
    assert [card.started_at for card in cards] == [cards[0].started_at] * 4
    assert [card.terminated_at for card in cards[:3]] == [None, None, None]
    assert cards[3].terminated_at == datetime(
        2026,
        7,
        30,
        8,
        0,
        3,
        tzinfo=timezone.utc,
    )


def test_bridge_result_identity_and_no_result_paths_map_without_current_reads(qapp):
    from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter

    dashboard = DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(dashboard, diagnostic_callback=diagnostics.append)
    initial_attempt = RUN_IDS[7]
    reanalysis_attempt = RUN_IDS[8]
    events = (
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            expected_revision=1,
            proposed_revision=2,
            operation_id=initial_attempt,
            result_attempt_id=initial_attempt,
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            run_id=RUN_IDS[1],
            expected_revision=1,
            proposed_revision=2,
            operation_id=RUN_IDS[6],
            conclusion=TerminalConclusion.NO_RESULT,
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_STARTED,
            expected_revision=2,
            proposed_revision=3,
            operation_id=reanalysis_attempt,
            conclusion=TerminalConclusion.OK,
            result_attempt_id=initial_attempt,
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_COMPLETED,
            expected_revision=3,
            proposed_revision=4,
            operation_id=reanalysis_attempt,
            conclusion=TerminalConclusion.NO_RESULT,
        ),
    )

    for event in events:
        adapter.publish(event)
    _drain(qapp)

    assert not diagnostics
    cards = dashboard.store.snapshot().terminal_cards
    assert all(card.conclusion is ResultConclusion.NO_RESULT for card in cards)
    assert all(
        dashboard.store.display_payload(card.run_id).review["no_result"] is True
        for card in cards
    )


@pytest.mark.parametrize(
    "event",
    [
        projected_event(
            HistoryFactKind.RECORDING,
            expected_revision=4,
            proposed_revision=5,
            operation_id=RUN_IDS[8],
        ),
        projected_event(
            HistoryFactKind.WAITING_ANALYSIS,
            expected_revision=ABSENT,
            proposed_revision=1,
        ),
        projected_event(
            HistoryFactKind.ANALYZING,
            expected_revision=3,
            proposed_revision=4,
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            conclusion=TerminalConclusion.ERROR,
            error_category=TerminalErrorCategory.PLC_INGRESS_REJECTED,
            reason="PLC ingress rejected",
            labels=history_labels(config_id=None, fault_types=()),
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            operation_id=None,
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            expected_revision=2,
            proposed_revision=3,
            conclusion=TerminalConclusion.ERROR,
            error_category=TerminalErrorCategory.PREFLIGHT_REJECTED,
            reason="late preaccept category",
        ),
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            expected_revision=2,
            proposed_revision=3,
            operation_id=RUN_IDS[7],
            conclusion=TerminalConclusion.NG,
            fault_type="异响",
            manual_revision=3,
            result_attempt_id=RUN_IDS[7],
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_STARTED,
            expected_revision=ABSENT,
            proposed_revision=1,
            operation_id=RUN_IDS[8],
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_COMPLETED,
            expected_revision=2,
            proposed_revision=3,
            operation_id=None,
        ),
        projected_event(
            HistoryFactKind.REANALYSIS_CANCELLED,
            expected_revision=2,
            proposed_revision=3,
            operation_id=None,
        ),
        projected_event(
            HistoryFactKind.TERMINAL_RESULT_REPLACED,
            expected_revision=ABSENT,
            proposed_revision=1,
            conclusion=TerminalConclusion.OK,
            manual_revision=1,
        ),
        projected_event(
            HistoryFactKind.TERMINAL_RESULT_REPLACED,
            expected_revision=2,
            proposed_revision=3,
            operation_id=RUN_IDS[8],
            conclusion=TerminalConclusion.OK,
            manual_revision=3,
        ),
        projected_event(
            HistoryFactKind.TERMINAL_RESULT_REPLACED,
            expected_revision=2,
            proposed_revision=3,
            conclusion=TerminalConclusion.OK,
        ),
        replace(
            projected_event(
                HistoryFactKind.RUN_TERMINATED,
                expected_revision=2,
                proposed_revision=3,
                operation_id=RUN_IDS[7],
                conclusion=TerminalConclusion.NG,
                result_attempt_id=RUN_IDS[7],
            ),
            projection=replace(
                projected_event(
                    HistoryFactKind.RUN_TERMINATED,
                    expected_revision=2,
                    proposed_revision=3,
                    operation_id=RUN_IDS[7],
                    conclusion=TerminalConclusion.NG,
                    result_attempt_id=RUN_IDS[7],
                ).projection,
                result_payload=bridge_result(RUN_IDS[7], judgment="PASS"),
            ),
        ),
        projected_event(
            HistoryFactKind.TERMINAL_RESULT_REPLACED,
            expected_revision=2,
            proposed_revision=3,
            conclusion=TerminalConclusion.ERROR,
            error_category=TerminalErrorCategory.ANALYSIS_FAILED,
            reason="not a manual result",
            manual_revision=3,
        ),
    ],
    ids=[
        "recording_non_absent_and_operation",
        "waiting_analysis_absent",
        "analyzing_without_operation",
        "plc_ingress_preaccept",
        "preaccept_success",
        "accepted_preaccept_category",
        "initial_manual_fault_fields",
        "reanalysis_start_absent",
        "reanalysis_complete_without_operation",
        "reanalysis_cancel_without_operation",
        "manual_replacement_absent",
        "manual_replacement_with_operation",
        "manual_replacement_without_manual_revision",
        "automatic_bridge_judgment_mismatch",
        "manual_replacement_error",
    ],
)
def test_semantically_invalid_exact_events_fail_before_qt_enqueue(qapp, event):
    from app.service.multi_run_integration.history_qt_adapter import (
        HistoryQtAdapter,
        HistoryQtAdapterDiagnosticCode,
    )

    dashboard = DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(dashboard, diagnostic_callback=diagnostics.append)

    adapter.publish(event)
    _drain(qapp)

    assert dashboard.calls == []
    assert dashboard.store.snapshot().terminal_cards == ()
    assert len(diagnostics) == 1
    assert diagnostics[0].code is HistoryQtAdapterDiagnosticCode.MAPPING_FAILED


@pytest.mark.parametrize(
    "payload",
    [
        FrozenPayload(
            "sha256:not-a-bridge-result",
            b'{"judgment":"PASS","result":{}}',
        ),
        FrozenPayload(
            "bridge-result:not-a-uuid",
            b'{"judgment":"PASS","result":{}}',
        ),
        FrozenPayload(
            f"bridge-result:{RUN_IDS[7]}",
            b'{"judgment": "PASS", "result": {}}',
        ),
        FrozenPayload(
            f"bridge-result:{RUN_IDS[7]}",
            b'{"judgment":"PASS","judgment":"PASS","result":{}}',
        ),
        FrozenPayload(
            f"bridge-result:{RUN_IDS[7]}",
            b'{"judgment":"MAYBE","result":{}}',
        ),
        FrozenPayload(
            f"bridge-result:{RUN_IDS[7]}",
            b'{"judgment":"PASS","other":{}}',
        ),
        bridge_result(RUN_IDS[6]),
    ],
    ids=[
        "foreign_identity_scheme",
        "malformed_attempt_identity",
        "noncanonical_json",
        "duplicate_json_key",
        "unknown_judgment",
        "wrong_root_shape",
        "operation_identity_mismatch",
    ],
)
def test_malformed_bridge_result_is_one_mapping_diagnostic_and_zero_enqueue(
    qapp,
    payload,
):
    from app.service.multi_run_integration.history_qt_adapter import (
        HistoryQtAdapter,
        HistoryQtAdapterDiagnosticCode,
    )

    base = projected_event(
        HistoryFactKind.RUN_TERMINATED,
        expected_revision=2,
        proposed_revision=3,
        operation_id=RUN_IDS[7],
        result_attempt_id=RUN_IDS[7],
    )
    event = replace(
        base,
        projection=replace(base.projection, result_payload=payload),
    )
    dashboard = DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(dashboard, diagnostic_callback=diagnostics.append)

    adapter.publish(event)
    _drain(qapp)

    assert dashboard.calls == []
    assert len(diagnostics) == 1
    assert diagnostics[0].code is HistoryQtAdapterDiagnosticCode.MAPPING_FAILED


def test_persistence_terminal_failure_is_plc_only_and_never_enqueued(qapp):
    from app.service.multi_run_integration.history_qt_adapter import (
        HistoryQtAdapter,
        HistoryQtAdapterDiagnosticCode,
    )

    event = projected_event(
        HistoryFactKind.RUN_TERMINATED,
        expected_revision=2,
        proposed_revision=3,
        operation_id=RUN_IDS[7],
        conclusion=TerminalConclusion.ERROR,
        error_category=TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
        reason="terminal persistence failed",
    )
    dashboard = DashboardProbe()
    diagnostics = []
    adapter = HistoryQtAdapter(dashboard, diagnostic_callback=diagnostics.append)

    adapter.publish(event)
    _drain(qapp)

    assert dashboard.calls == []
    assert dashboard.store.snapshot().terminal_cards == ()
    assert len(diagnostics) == 1
    assert diagnostics[0].code is HistoryQtAdapterDiagnosticCode.MAPPING_FAILED


def test_post_store_projection_failure_keeps_eviction_and_disables_manual_target(
    qapp,
    monkeypatch,
):
    from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter

    dashboard = ResultDashboardModule(
        SimpleNamespace(config_service=SimpleNamespace())
    )
    evictions = []
    adapter_diagnostics = []
    projection_diagnostics = []
    targets = []
    requests = []
    reanalysis_requests = []
    dashboard.history_diagnostic.connect(projection_diagnostics.append)
    dashboard.manual_judgment_target_changed.connect(targets.append)
    dashboard.manual_judgment_requested.connect(requests.append)
    dashboard.reanalysis_requested.connect(reanalysis_requests.append)
    adapter = HistoryQtAdapter(
        dashboard,
        eviction_callback=evictions.append,
        diagnostic_callback=adapter_diagnostics.append,
    )

    for run_id in RUN_IDS[:6]:
        adapter.publish(
            projected_event(
                HistoryFactKind.RUN_TERMINATED,
                run_id=run_id,
                expected_revision=1,
                proposed_revision=2,
                operation_id=run_id,
                conclusion=TerminalConclusion.ERROR,
                error_category=TerminalErrorCategory.ANALYSIS_FAILED,
                reason="analysis failed",
            )
        )
    _drain(qapp)
    assert targets[-1].run_id == RUN_IDS[5]

    def fail_projection(*_args):
        raise RuntimeError("widget projection failed")

    monkeypatch.setattr(dashboard, "_show_error", fail_projection)
    adapter.publish(
        projected_event(
            HistoryFactKind.RUN_TERMINATED,
            run_id=RUN_IDS[6],
            expected_revision=1,
            proposed_revision=2,
            operation_id=RUN_IDS[6],
            conclusion=TerminalConclusion.ERROR,
            error_category=TerminalErrorCategory.ANALYSIS_FAILED,
            reason="analysis failed",
        )
    )
    _drain(qapp)

    assert [card.run_id for card in dashboard.history_snapshot().terminal_cards] == list(
        reversed(RUN_IDS[1:7])
    )
    assert [notice.run_id for notice in evictions] == [RUN_IDS[0]]
    assert adapter_diagnostics == []
    assert len(projection_diagnostics) == 1
    assert (
        projection_diagnostics[0].code
        is HistoryProjectionDiagnosticCode.PROJECTION_FAILED
    )
    assert targets[-1] is None
    assert not any(
        target is not None and target.run_id == RUN_IDS[6]
        for target in targets
    )

    result = dashboard.request_manual_judgment("OK")
    assert type(result) is ResultOperationRejected
    assert result.code is ResultOperationRejectionCode.OPERATION_NOT_ALLOWED
    assert requests == []

    reanalysis_result = dashboard.request_reanalysis(0.0, 1.0)
    assert type(reanalysis_result) is ResultOperationRejected
    assert (
        reanalysis_result.code
        is ResultOperationRejectionCode.OPERATION_NOT_ALLOWED
    )
    assert reanalysis_requests == []


def test_destroyed_target_queue_store_and_projection_failures_are_diagnostic_only(qapp):
    from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter

    diagnostics = []
    dashboard = DashboardProbe()
    adapter = HistoryQtAdapter(dashboard, diagnostic_callback=diagnostics.append)

    event = projected_event(HistoryFactKind.RECORDING)
    malformed = replace(
        event,
        projection=replace(
            event.projection,
            history_labels=replace(history_labels(), canonical_bytes=b"{}"),
        ),
    )
    adapter.publish(malformed)
    assert len(diagnostics) == 1
    assert dashboard.calls == []

    adapter._enqueue = lambda _event: (_ for _ in ()).throw(RuntimeError("queue failed"))
    adapter.publish(event)
    assert len(diagnostics) == 2
    assert dashboard.calls == []

    dashboard.raise_on_apply = True
    adapter._enqueue = adapter._qt_enqueue
    adapter.publish(event)
    _drain(qapp)
    assert len(diagnostics) == 3
    assert len(dashboard.calls) == 1

    adapter._target_ref = lambda: None
    adapter.publish(event)
    _drain(qapp)
    assert len(diagnostics) == 4
    assert len(dashboard.calls) == 1
    assert all(len(item.detail) <= 240 for item in diagnostics)


def test_exact_preaccept_classification_and_seventh_card_eviction_are_ordered(qapp):
    from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter

    dashboard = DashboardProbe()
    evictions = []
    diagnostics = []
    adapter = HistoryQtAdapter(
        dashboard,
        eviction_callback=evictions.append,
        diagnostic_callback=diagnostics.append,
    )
    preaccept = (
        (TerminalConclusion.ERROR, TerminalErrorCategory.PREFLIGHT_REJECTED),
        (TerminalConclusion.ERROR, TerminalErrorCategory.INVALID_FROZEN_INPUT),
        (
            TerminalConclusion.CAPACITY_REJECTED,
            TerminalErrorCategory.CAPTURE_CAPACITY,
        ),
        (TerminalConclusion.ERROR, TerminalErrorCategory.RESIDENT_UNAVAILABLE),
        (TerminalConclusion.ERROR, TerminalErrorCategory.ACTIVE_COMMIT_FAILED),
    )
    unresolved = history_labels(config_id=None, fault_types=())
    for index, (conclusion, category) in enumerate(preaccept):
        adapter.publish(
            projected_event(
                HistoryFactKind.RUN_TERMINATED,
                run_id=RUN_IDS[index],
                conclusion=conclusion,
                error_category=category,
                reason=category.name,
                labels=unresolved,
            )
        )
    _drain(qapp)

    assert [
        card.conclusion for card in dashboard.store.snapshot().terminal_cards
    ] == [
        ResultConclusion.ERROR,
        ResultConclusion.ERROR,
        ResultConclusion.CAPACITY_REJECTED,
        ResultConclusion.ERROR,
        ResultConclusion.ERROR,
    ]
    assert not diagnostics

    dashboard = DashboardProbe()
    evictions.clear()
    adapter = HistoryQtAdapter(
        dashboard,
        eviction_callback=evictions.append,
        diagnostic_callback=diagnostics.append,
    )
    for run_id in RUN_IDS[:7]:
        adapter.publish(
            projected_event(
                HistoryFactKind.RUN_TERMINATED,
                run_id=run_id,
                expected_revision=1,
                proposed_revision=2,
                operation_id=run_id,
                result_attempt_id=run_id,
            )
        )
    _drain(qapp)

    snapshot = dashboard.store.snapshot()
    assert [card.run_id for card in snapshot.terminal_cards] == list(
        reversed(RUN_IDS[1:7])
    )
    assert [notice.run_id for notice in evictions] == [RUN_IDS[0]]
