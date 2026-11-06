from __future__ import annotations

from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from app.service.result_history import ActiveRunRemoved
from app.service.result_history_contracts import (
    HISTORY_MAPPING_CONTRACT,
    HistoryMappingContractRow,
    HistoryMappingRejection,
    HistoryMappingRejectionCode,
    ManualJudgmentRequest,
    ManualJudgmentTarget,
    ReanalysisCancelRequest,
    ReanalysisRequest,
    ResultOperationAccepted,
    ResultOperationRejected,
    ResultOperationRejectionCode,
)


EXPECTED_ROWS = (
    ("run accepted / recording", "ActiveRunChanged", "recording", None, "none"),
    ("capture complete / persisting", "ActiveRunChanged", "waiting_save", None, "none"),
    ("persisted / analysis queued", "ActiveRunChanged", "waiting_analysis", None, "none"),
    ("analysis started", "ActiveRunChanged", "analyzing", None, "none"),
    ("terminal OK", "RunTerminated", "terminal", "ok", "ReviewDisplayPayload"),
    ("terminal NG", "RunTerminated", "terminal", "ng", "ReviewDisplayPayload"),
    ("terminal no-result", "RunTerminated", "terminal", "no_result", "ReviewDisplayPayload"),
    ("terminal analysis/system error", "RunTerminated", "terminal", "error", "ErrorDisplayPayload"),
    ("admission capacity rejected", "RunTerminated", "terminal", "capacity_rejected", "ErrorDisplayPayload"),
    ("terminal manual stop", "RunTerminated", "terminal", "manual_stopped", "ErrorDisplayPayload"),
    ("accepted reanalysis start", "ReanalysisStarted", "reanalyzing", "retain prior conclusion", "existing backup retained"),
    ("reanalysis OK", "ReanalysisCompleted", "terminal", "ok", "ReviewDisplayPayload"),
    ("reanalysis NG", "ReanalysisCompleted", "terminal", "ng", "ReviewDisplayPayload"),
    ("reanalysis no-result", "ReanalysisCompleted", "terminal", "no_result", "ReviewDisplayPayload"),
    ("reanalysis error", "ReanalysisCompleted", "terminal", "error", "ErrorDisplayPayload"),
    ("accepted reanalysis cancel", "ReanalysisCancelled", "restore prior terminal card", None, "restore prior payload"),
    ("accepted manual judgment OK", "TerminalResultReplaced", "terminal", "ok", "ReviewDisplayPayload"),
    ("accepted manual judgment NG", "TerminalResultReplaced", "terminal", "ng", "ReviewDisplayPayload"),
    ("active run withdrawn before terminal", "ActiveRunRemoved", "removed", None, "none"),
)


def test_history_mapping_contract_contains_exact_19_rows_in_spec_order():
    assert type(HISTORY_MAPPING_CONTRACT) is tuple
    assert tuple(
        (
            row.domain_fact,
            row.event_name,
            row.card_phase,
            row.card_conclusion,
            row.payload_kind,
        )
        for row in HISTORY_MAPPING_CONTRACT
    ) == EXPECTED_ROWS


def test_each_mapping_row_freezes_domain_event_phase_conclusion_and_payload_cells():
    assert all(type(row) is HistoryMappingContractRow for row in HISTORY_MAPPING_CONTRACT)
    with pytest.raises(FrozenInstanceError):
        HISTORY_MAPPING_CONTRACT[0].event_name = "RunTerminated"


def test_history_mapping_rejection_exposes_exact_five_codes_and_return_union_shape():
    assert [item.value for item in HistoryMappingRejectionCode] == [
        "unknown_phase",
        "unknown_outcome",
        "invalid_phase_outcome",
        "missing_required_payload",
        "identity_mismatch",
    ]
    assert type(ActiveRunRemoved("run-1")) is ActiveRunRemoved
    assert type(
        HistoryMappingRejection(
            HistoryMappingRejectionCode.UNKNOWN_PHASE,
            "run-1",
            "mystery",
            None,
            "unknown phase",
        )
    ) is HistoryMappingRejection


@pytest.mark.parametrize(
    "factory,args",
    [
        (HistoryMappingContractRow, ("x", "Mystery", "terminal", None, "none")),
        (HistoryMappingContractRow, ("x", "RunTerminated", "terminal", None, "Mystery")),
        (HistoryMappingRejection, ("unknown_phase", None, None, None, "detail")),
        (HistoryMappingRejection, (HistoryMappingRejectionCode.UNKNOWN_PHASE, "", None, None, "detail")),
        (HistoryMappingRejection, (HistoryMappingRejectionCode.UNKNOWN_PHASE, None, None, None, " ")),
        (HistoryMappingRejection, (HistoryMappingRejectionCode.UNKNOWN_PHASE, None, None, None, "x" * 241)),
    ],
)
def test_mapping_contract_and_rejection_constructors_enforce_exact_types_and_bounded_detail(factory, args):
    with pytest.raises(ValueError):
        factory(*args)


def test_unknown_semantic_values_can_only_be_represented_by_typed_rejection():
    rejection = HistoryMappingRejection(
        HistoryMappingRejectionCode.INVALID_PHASE_OUTCOME,
        "run-1",
        "recording",
        "ok",
        "recording cannot carry an outcome",
    )
    assert type(rejection) is HistoryMappingRejection
    assert not hasattr(__import__("app.service.result_history_contracts", fromlist=["x"]), "map_history_event")


def test_contract_module_imports_no_multirun_enum_or_adapter():
    source = __import__("inspect").getsource(
        __import__("app.service.result_history_contracts", fromlist=["x"])
    )
    assert "multi_run" not in source.lower()
    assert "MultiRun" not in source


def test_operation_contract_constructors_enforce_exact_types_and_bounded_reason():
    manual = ManualJudgmentRequest("run-1", "NG", 2, "异响")
    reanalysis = ReanalysisRequest("run-1", 2, 0.0, 1.0)
    cancel = ReanalysisCancelRequest("run-1", "operation-1", 3)
    assert ResultOperationAccepted(manual).request is manual
    assert ResultOperationAccepted(reanalysis).request is reanalysis
    assert ResultOperationAccepted(cancel).request is cancel
    with pytest.raises(ValueError):
        ManualJudgmentRequest("run-1", "", 0, None)
    with pytest.raises(ValueError):
        ReanalysisCancelRequest("run-1", "", 0)
    with pytest.raises(ValueError):
        ResultOperationAccepted(ActiveRunRemoved("run-1"))
    with pytest.raises(ValueError):
        ResultOperationRejected(ResultOperationRejectionCode.NO_SELECTION, " ")
    with pytest.raises(ValueError):
        ResultOperationRejected(ResultOperationRejectionCode.NO_SELECTION, "x" * 241)


def test_manual_fault_contracts_are_exact_frozen_values():
    target = ManualJudgmentTarget("run-1", 2, ("异响", "漏装"), True)
    assert target.fault_types == ("异响", "漏装")
    assert ManualJudgmentRequest("run-1", "OK", 2, None).fault_type is None
    with pytest.raises(ValueError):
        ManualJudgmentTarget("run-1", 2, ["异响"], True)
    with pytest.raises(ValueError):
        ManualJudgmentRequest("run-1", "OK", 2, "异响")


def test_operation_rejection_codes_cover_invalid_no_selection_active_forbidden_and_cancel_states():
    assert [item.value for item in ResultOperationRejectionCode] == [
        "invalid_argument",
        "no_selection",
        "target_not_terminal",
        "operation_not_allowed",
        "not_reanalyzing",
        "missing_operation_id",
    ]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (True, 1.0),
        (0.0, False),
        (nan, 1.0),
        (0.0, inf),
        (-1.0, 1.0),
        (1.0, 1.0),
        (2.0, 1.0),
    ],
)
def test_reanalysis_window_rejects_bool_nan_infinity_negative_equal_and_reversed_values(start, end):
    with pytest.raises(ValueError):
        ReanalysisRequest("run-1", 0, start, end)
