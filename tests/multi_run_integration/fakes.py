from __future__ import annotations

import json

from app.service.fault_types import encode_history_labels_payload
from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    FrozenPayload,
    HistoryFactKind,
    ProjectedHistoryEvent,
    ResultProjection,
    ResultProjectionPhase,
    TerminalConclusion,
    TerminalErrorCategory,
)


RUN_IDS = tuple(
    f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 10)
)


def bridge_result(
    analysis_attempt_id: str,
    *,
    judgment: str = "PASS",
) -> FrozenPayload:
    value = {"judgment": judgment, "result": {}}
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return FrozenPayload(
        value_id=f"bridge-result:{analysis_attempt_id}",
        canonical_bytes=canonical,
    )


def history_labels(
    *,
    config_id: int | None = 7,
    fault_types: tuple[str, ...] = ("异响", "漏装"),
    manual_judgment_buttons_visible: bool = False,
    sn: str = "SN-1",
) -> FrozenPayload:
    value_id, canonical = encode_history_labels_payload(
        config_id=config_id,
        name="Frozen config",
        fault_types=fault_types,
        manual_judgment_buttons_visible=manual_judgment_buttons_visible,
        details=({"label": "Channel", "value": "0"},),
        sn=sn,
        trigger_source="manual",
    )
    return FrozenPayload(value_id=value_id, canonical_bytes=canonical)


def projected_event(
    kind: HistoryFactKind,
    *,
    run_id: str = RUN_IDS[0],
    expected_revision=ABSENT,
    proposed_revision: int = 1,
    operation_id: str | None = None,
    conclusion: TerminalConclusion | None = None,
    error_category: TerminalErrorCategory | None = None,
    reason: str = "",
    fault_type: str | None = None,
    manual_revision: int | None = None,
    result_attempt_id: str | None = None,
    labels: FrozenPayload | None = None,
) -> ProjectedHistoryEvent:
    phases = {
        HistoryFactKind.RECORDING: ResultProjectionPhase.RECORDING,
        HistoryFactKind.WAITING_SAVE: ResultProjectionPhase.WAITING_SAVE,
        HistoryFactKind.WAITING_ANALYSIS: ResultProjectionPhase.WAITING_ANALYSIS,
        HistoryFactKind.ANALYZING: ResultProjectionPhase.ANALYZING,
        HistoryFactKind.REANALYSIS_STARTED: ResultProjectionPhase.REANALYZING,
        HistoryFactKind.RUN_TERMINATED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.REANALYSIS_COMPLETED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.REANALYSIS_CANCELLED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.TERMINAL_RESULT_REPLACED: ResultProjectionPhase.TERMINAL,
    }
    if conclusion is None and phases[kind] in {
        ResultProjectionPhase.REANALYZING,
        ResultProjectionPhase.TERMINAL,
    }:
        conclusion = TerminalConclusion.OK
    payload = (
        bridge_result(
            result_attempt_id or operation_id or RUN_IDS[8],
            judgment=(
                "FAIL"
                if conclusion is TerminalConclusion.NG
                else "PASS"
            ),
        )
        if conclusion in {TerminalConclusion.OK, TerminalConclusion.NG}
        else None
    )
    return ProjectedHistoryEvent(
        kind=kind,
        run_id=run_id,
        expected_revision=expected_revision,
        operation_id=operation_id,
        proposed_revision=proposed_revision,
        projection=ResultProjection(
            phase=phases[kind],
            conclusion=conclusion,
            error_category=error_category,
            reason=reason,
            history_labels=labels or history_labels(),
            result_payload=payload,
            fault_type=fault_type,
            manual_judgment_revision=manual_revision,
        ),
    )
