from __future__ import annotations

import json
import weakref
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from PyQt6 import sip
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal

from app.service.fault_types import decode_history_labels_payload
from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    FrozenPayload,
    HistoryEvictionNotice,
    HistoryFactKind,
    ProjectedHistoryEvent,
    ResultProjection,
    ResultEnrichmentProjection,
    ResultProjectionPhase,
    TerminalConclusion,
    TerminalErrorCategory,
)
from app.service.multi_run_coordinator_mvp.contracts import require_uuid4
from app.service.result_history import (
    ActiveRunChanged,
    ConfigSummaryField,
    ErrorDisplayPayload,
    FrozenConfigSummary,
    ReanalysisCancelled,
    ReanalysisCompleted,
    ReanalysisStarted,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultPhase,
    ReviewDisplayPayload,
    RunTerminated,
    TerminalResultReplaced,
)


class HistoryQtAdapterDiagnosticCode(str, Enum):
    MAPPING_FAILED = "mapping_failed"
    QUEUE_FAILED = "queue_failed"
    TARGET_DESTROYED = "target_destroyed"
    WRONG_QT_THREAD = "wrong_qt_thread"
    APPLY_FAILED = "apply_failed"
    EVICTION_CALLBACK_FAILED = "eviction_callback_failed"


@dataclass(frozen=True)
class HistoryQtAdapterDiagnostic:
    code: HistoryQtAdapterDiagnosticCode
    run_id: str | None
    detail: str

    def __post_init__(self) -> None:
        if type(self.code) is not HistoryQtAdapterDiagnosticCode:
            raise ValueError("code must be an exact adapter diagnostic code")
        if self.run_id is not None and (
            type(self.run_id) is not str or not self.run_id.strip()
        ):
            raise ValueError("run_id must be a non-empty string or None")
        if (
            type(self.detail) is not str
            or not self.detail.strip()
            or len(self.detail) > 240
        ):
            raise ValueError("detail must contain 1..240 characters")


@dataclass(frozen=True)
class _DecodedBridgeResult:
    analysis_attempt_id: str
    judgment: str
    value: dict[str, object]


@dataclass(frozen=True)
class _MappedEnrichment:
    run_id: str
    attempt_id: str
    terminal_revision: str
    expected_revision: int
    review_payload: ReviewDisplayPayload


_PREACCEPT_COMBINATIONS = {
    TerminalErrorCategory.PREFLIGHT_REJECTED: TerminalConclusion.ERROR,
    TerminalErrorCategory.INVALID_FROZEN_INPUT: TerminalConclusion.ERROR,
    TerminalErrorCategory.CAPTURE_CAPACITY: TerminalConclusion.CAPACITY_REJECTED,
    TerminalErrorCategory.RESIDENT_UNAVAILABLE: TerminalConclusion.ERROR,
    TerminalErrorCategory.ACTIVE_COMMIT_FAILED: TerminalConclusion.ERROR,
}
_FORBIDDEN_ACCEPTED_PREACCEPT_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.INVALID_REQUEST,
        TerminalErrorCategory.PREFLIGHT_REJECTED,
        TerminalErrorCategory.INVALID_FROZEN_INPUT,
        TerminalErrorCategory.PLC_INGRESS_REJECTED,
        TerminalErrorCategory.CAPTURE_CAPACITY,
        TerminalErrorCategory.RESIDENT_UNAVAILABLE,
        TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
        TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
    }
)


class HistoryQtAdapter(QObject):
    """One-attempt Coordinator-to-Dashboard queued history boundary."""

    diagnostic = pyqtSignal(object)
    _queued_event = pyqtSignal(object)

    def __init__(
        self,
        dashboard: object,
        *,
        eviction_callback: Callable[[HistoryEvictionNotice], object] | None = None,
        diagnostic_callback: Callable[[HistoryQtAdapterDiagnostic], object]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        if not callable(getattr(dashboard, "apply_history_event", None)):
            raise TypeError("dashboard must expose apply_history_event")
        if not callable(getattr(dashboard, "thread", None)):
            raise TypeError("dashboard must expose its Qt thread")
        if eviction_callback is not None and not callable(eviction_callback):
            raise TypeError("eviction_callback must be callable or None")
        if diagnostic_callback is not None and not callable(diagnostic_callback):
            raise TypeError("diagnostic_callback must be callable or None")
        self._target_ref = weakref.ref(dashboard)
        self._target_thread = dashboard.thread()
        self._eviction_callback = eviction_callback
        self._diagnostic_callback = diagnostic_callback
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started_at_by_run: dict[str, datetime] = {}
        self._terminated_at_by_run: dict[str, datetime] = {}
        self._queued_event.connect(
            self._deliver,
            Qt.ConnectionType.QueuedConnection,
        )
        self._enqueue = self._qt_enqueue

    def publish(self, event: ProjectedHistoryEvent) -> None:
        run_id = getattr(event, "run_id", None)
        try:
            mapped = self._map_event(event)
        except Exception as exc:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.MAPPING_FAILED,
                run_id if type(run_id) is str else None,
                exc,
            )
            return
        try:
            self._enqueue(mapped)
        except Exception as exc:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.QUEUE_FAILED,
                event.run_id,
                exc,
            )

    def publish_enrichment(self, event: ResultEnrichmentProjection) -> None:
        run_id = getattr(event, "run_id", None)
        try:
            mapped = self._map_enrichment(event)
        except Exception as exc:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.MAPPING_FAILED,
                run_id if type(run_id) is str else None,
                exc,
            )
            return
        try:
            self._enqueue(mapped)
        except Exception as exc:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.QUEUE_FAILED,
                event.run_id,
                exc,
            )

    def _qt_enqueue(self, event: object) -> None:
        self._queued_event.emit(event)

    def _deliver(self, event: object) -> None:
        target = self._target_ref()
        run_id = _event_run_id(event)
        if target is None or (isinstance(target, QObject) and sip.isdeleted(target)):
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.TARGET_DESTROYED,
                run_id,
                "dashboard target was destroyed before queued delivery",
            )
            return
        if QThread.currentThread() is not self._target_thread:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.WRONG_QT_THREAD,
                run_id,
                "queued history delivery ran outside the Dashboard thread",
            )
            return
        try:
            if type(event) is _MappedEnrichment:
                target.apply_enrichment_projection(event)
                return
            transition = target.apply_history_event(event)
        except Exception as exc:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.APPLY_FAILED,
                run_id,
                exc,
            )
            return
        evicted_run_id = getattr(transition, "evicted_run_id", None)
        if evicted_run_id is None:
            return
        self._started_at_by_run.pop(evicted_run_id, None)
        self._terminated_at_by_run.pop(evicted_run_id, None)
        if self._eviction_callback is None:
            return
        try:
            self._eviction_callback(HistoryEvictionNotice(evicted_run_id))
        except Exception as exc:
            self._diagnose(
                HistoryQtAdapterDiagnosticCode.EVICTION_CALLBACK_FAILED,
                evicted_run_id,
                exc,
            )

    @staticmethod
    def _map_enrichment(
        event: ResultEnrichmentProjection,
    ) -> _MappedEnrichment:
        if type(event) is not ResultEnrichmentProjection:
            raise TypeError(
                "enrichment event must be an exact ResultEnrichmentProjection"
            )
        event.__post_init__()
        decoded = _decode_bridge_result(event.result_payload)
        if decoded.analysis_attempt_id != event.attempt_id:
            raise ValueError(
                "enrichment payload must match the projection attempt"
            )
        review = deepcopy(decoded.value["result"])
        if type(review) is not dict:
            raise ValueError("enrichment result must be an object")
        dashboard_config = review.get("config_payload")
        if type(dashboard_config) is not dict:
            raise ValueError(
                "enrichment result requires its frozen config payload"
            )
        return _MappedEnrichment(
            run_id=event.run_id,
            attempt_id=event.attempt_id,
            terminal_revision=event.terminal_revision,
            expected_revision=event.expected_revision,
            review_payload=ReviewDisplayPayload(
                review=review,
                dashboard_config=dashboard_config,
            ),
        )

    def _diagnose(
        self,
        code: HistoryQtAdapterDiagnosticCode,
        run_id: str | None,
        error: object,
    ) -> None:
        detail = str(error).strip() or type(error).__name__
        diagnostic = HistoryQtAdapterDiagnostic(code, run_id, detail[:240])
        if self._diagnostic_callback is not None:
            try:
                self._diagnostic_callback(diagnostic)
            except Exception:
                pass
            return
        try:
            self.diagnostic.emit(diagnostic)
        except Exception:
            pass

    def _map_event(self, event: ProjectedHistoryEvent):
        if type(event) is not ProjectedHistoryEvent:
            raise TypeError("history event must be an exact ProjectedHistoryEvent")
        if type(event.projection) is not ResultProjection:
            raise TypeError("projection must be an exact ResultProjection")
        if type(event.projection.history_labels) is not FrozenPayload:
            raise TypeError("history_labels must be an exact FrozenPayload")
        if (
            event.projection.result_payload is not None
            and type(event.projection.result_payload) is not FrozenPayload
        ):
            raise TypeError("result_payload must be an exact FrozenPayload")
        event.__post_init__()
        event.projection.__post_init__()
        labels = decode_history_labels_payload(
            value_id=event.projection.history_labels.value_id,
            canonical_bytes=event.projection.history_labels.canonical_bytes,
        )
        config_value = labels["config"]
        decoded_result = _validate_event_semantics(
            event,
            fault_types=tuple(config_value["fault_types"]),
        )
        config = FrozenConfigSummary(
            config_id=config_value["config_id"],
            name=config_value["name"],
            details=tuple(
                ConfigSummaryField(item["label"], item["value"])
                for item in config_value["details"]
            ),
            fault_types=tuple(config_value["fault_types"]),
        )
        kind = event.kind
        if kind is HistoryFactKind.RECORDING:
            return ActiveRunChanged(self._card(event, config, labels))
        if kind is HistoryFactKind.WAITING_SAVE:
            return ActiveRunChanged(self._card(event, config, labels))
        if kind is HistoryFactKind.WAITING_ANALYSIS:
            return ActiveRunChanged(self._card(event, config, labels))
        if kind is HistoryFactKind.ANALYZING:
            return ActiveRunChanged(self._card(event, config, labels))
        if kind is HistoryFactKind.RUN_TERMINATED:
            card = self._card(event, config, labels)
            payload = self._payload(event, config_value, decoded_result)
            return RunTerminated(
                card,
                review_payload=(
                    payload if type(payload) is ReviewDisplayPayload else None
                ),
                error_payload=(
                    payload if type(payload) is ErrorDisplayPayload else None
                ),
            )
        if kind is HistoryFactKind.REANALYSIS_STARTED:
            return ReanalysisStarted(
                event.run_id,
                _required_operation_id(event),
                _required_expected_revision(event),
            )
        if kind is HistoryFactKind.REANALYSIS_COMPLETED:
            card = self._card(event, config, labels)
            payload = self._payload(event, config_value, decoded_result)
            return ReanalysisCompleted(
                event.run_id,
                _required_operation_id(event),
                _required_expected_revision(event),
                card,
                review_payload=(
                    payload if type(payload) is ReviewDisplayPayload else None
                ),
                error_payload=(
                    payload if type(payload) is ErrorDisplayPayload else None
                ),
            )
        if kind is HistoryFactKind.REANALYSIS_CANCELLED:
            return ReanalysisCancelled(
                event.run_id,
                _required_operation_id(event),
                _required_expected_revision(event),
            )
        if kind is HistoryFactKind.TERMINAL_RESULT_REPLACED:
            card = self._card(event, config, labels)
            payload = self._payload(event, config_value, decoded_result)
            return TerminalResultReplaced(
                event.run_id,
                _required_expected_revision(event),
                card,
                review_payload=(
                    payload if type(payload) is ReviewDisplayPayload else None
                ),
                error_payload=(
                    payload if type(payload) is ErrorDisplayPayload else None
                ),
            )
        raise ValueError("unknown HistoryFactKind")

    def _card(
        self,
        event: ProjectedHistoryEvent,
        config: FrozenConfigSummary,
        labels: dict[str, object],
    ) -> ResultHistoryCardSnapshot:
        phase = {
            ResultProjectionPhase.RECORDING: ResultPhase.RECORDING,
            ResultProjectionPhase.WAITING_SAVE: ResultPhase.WAITING_SAVE,
            ResultProjectionPhase.WAITING_ANALYSIS: ResultPhase.WAITING_ANALYSIS,
            ResultProjectionPhase.ANALYZING: ResultPhase.ANALYZING,
            ResultProjectionPhase.REANALYZING: ResultPhase.REANALYZING,
            ResultProjectionPhase.TERMINAL: ResultPhase.TERMINAL,
        }[event.projection.phase]
        conclusion = (
            None
            if event.projection.conclusion is None
            else {
                TerminalConclusion.OK: ResultConclusion.OK,
                TerminalConclusion.NG: ResultConclusion.NG,
                TerminalConclusion.NO_RESULT: ResultConclusion.NO_RESULT,
                TerminalConclusion.ERROR: ResultConclusion.ERROR,
                TerminalConclusion.CAPACITY_REJECTED: ResultConclusion.CAPACITY_REJECTED,
                TerminalConclusion.MANUAL_STOPPED: ResultConclusion.MANUAL_STOPPED,
            }[event.projection.conclusion]
        )
        now = self._clock()
        if type(now) is not datetime:
            raise TypeError("clock must return an exact datetime")
        active = phase in {
            ResultPhase.RECORDING,
            ResultPhase.WAITING_SAVE,
            ResultPhase.WAITING_ANALYSIS,
            ResultPhase.ANALYZING,
        }
        started_at = self._started_at_by_run.setdefault(event.run_id, now)
        terminated_at = (
            None
            if active
            else self._terminated_at_by_run.setdefault(event.run_id, now)
        )
        return ResultHistoryCardSnapshot(
            run_id=event.run_id,
            config=config,
            sn=labels["sn"],
            trigger_source=labels["trigger_source"],
            started_at=started_at,
            terminated_at=terminated_at,
            phase=phase,
            conclusion=conclusion,
            reason=event.projection.reason or None,
            is_active_run=active,
            manual_judgment_allowed=(
                not active and phase is ResultPhase.TERMINAL
            ),
            manual_reanalysis_allowed=(
                not active and phase is ResultPhase.TERMINAL
            ),
            revision=event.proposed_revision,
            operation_id=(
                event.operation_id
                if phase is ResultPhase.REANALYZING
                else None
            ),
            fault_type=event.projection.fault_type,
            manual_judgment_revision=(
                event.projection.manual_judgment_revision
            ),
        )

    @staticmethod
    def _payload(
        event: ProjectedHistoryEvent,
        config_value: dict[str, object],
        decoded_result: _DecodedBridgeResult | None,
    ):
        conclusion = event.projection.conclusion
        if conclusion is TerminalConclusion.NO_RESULT:
            return ReviewDisplayPayload(
                review={"no_result": True},
                dashboard_config={
                    "config_id": config_value["config_id"],
                    "config_name": config_value["name"],
                },
            )
        if conclusion in {
            TerminalConclusion.OK,
            TerminalConclusion.NG,
        }:
            review = deepcopy(
                decoded_result.value["result"]
                if decoded_result is not None
                else {}
            )
            if (
                decoded_result is not None
                and event.operation_id == decoded_result.analysis_attempt_id
                and event.projection.terminal_revision is not None
            ):
                review["_projection_identity"] = {
                    "attempt_id": decoded_result.analysis_attempt_id,
                    "terminal_revision": event.projection.terminal_revision,
                    "history_revision": event.proposed_revision,
                }
            if (
                isinstance(review, dict)
                and event.projection.manual_judgment_revision is not None
            ):
                summary = review.get("summary_payload")
                if isinstance(summary, dict):
                    if conclusion is TerminalConclusion.OK:
                        summary["overall_status"] = "pass"
                        summary["overall_label"] = "通过"
                    else:
                        summary["overall_status"] = "fail"
                        summary["overall_label"] = (
                            f"不通过（{event.projection.fault_type}）"
                            if event.projection.fault_type
                            else "不通过"
                        )
            dashboard_config = (
                review.get("config_payload")
                if isinstance(review, dict)
                and isinstance(review.get("config_payload"), dict)
                else None
            )
            return ReviewDisplayPayload(
                review=review,
                dashboard_config=(
                    dashboard_config
                    if dashboard_config is not None
                    else {
                        "config_id": config_value["config_id"],
                        "config_name": config_value["name"],
                    }
                ),
            )
        category = event.projection.error_category
        return ErrorDisplayPayload(
            error_category=(
                category.name.lower() if category is not None else conclusion.name.lower()
            ),
            reason=event.projection.reason or conclusion.name.lower(),
            context=tuple(
                ConfigSummaryField(item["label"], item["value"])
                for item in config_value["details"]
            ),
        )


def _required_expected_revision(event: ProjectedHistoryEvent) -> int:
    if event.expected_revision is ABSENT:
        raise ValueError("operation history event requires expected_revision")
    return event.expected_revision


def _required_operation_id(event: ProjectedHistoryEvent) -> str:
    if event.operation_id is None:
        raise ValueError("operation history event requires operation_id")
    return event.operation_id


def _event_run_id(event: object) -> str | None:
    card = getattr(event, "card", None)
    run_id = getattr(card, "run_id", None)
    if type(run_id) is str:
        return run_id
    run_id = getattr(event, "run_id", None)
    return run_id if type(run_id) is str else None


def _validate_event_semantics(
    event: ProjectedHistoryEvent,
    *,
    fault_types: tuple[str, ...],
) -> _DecodedBridgeResult | None:
    kind = event.kind
    projection = event.projection
    expected_absent = event.expected_revision is ABSENT
    operation_present = event.operation_id is not None
    decoded = (
        None
        if projection.result_payload is None
        else _decode_bridge_result(projection.result_payload)
    )

    if projection.fault_type is not None and projection.fault_type not in fault_types:
        raise ValueError("fault_type is not in the frozen history-label tuple")

    if kind is HistoryFactKind.RECORDING:
        if not expected_absent or operation_present:
            raise ValueError(
                "RECORDING requires ABSENT revision and no operation identity"
            )
        return _require_active_projection_without_result(projection, decoded)

    if kind is HistoryFactKind.WAITING_SAVE:
        if operation_present:
            raise ValueError("WAITING_SAVE must not include operation identity")
        return _require_active_projection_without_result(projection, decoded)

    if kind is HistoryFactKind.WAITING_ANALYSIS:
        if expected_absent or operation_present:
            raise ValueError(
                "WAITING_ANALYSIS requires a revision and no operation identity"
            )
        return _require_active_projection_without_result(projection, decoded)

    if kind is HistoryFactKind.ANALYZING:
        if expected_absent or not operation_present:
            raise ValueError(
                "ANALYZING requires a revision and operation identity"
            )
        return _require_active_projection_without_result(projection, decoded)

    if kind is HistoryFactKind.RUN_TERMINATED:
        if projection.fault_type is not None or (
            projection.manual_judgment_revision is not None
        ):
            raise ValueError(
                "initial terminal history cannot contain manual-only fields"
            )
        if expected_absent:
            if operation_present or decoded is not None:
                raise ValueError(
                    "preaccept terminal cannot include operation/result identity"
                )
            expected_conclusion = _PREACCEPT_COMBINATIONS.get(
                projection.error_category
            )
            if expected_conclusion is not projection.conclusion:
                raise ValueError(
                    "preaccept terminal is outside the exact five categories"
                )
            return None
        if projection.error_category in _FORBIDDEN_ACCEPTED_PREACCEPT_CATEGORIES:
            raise ValueError(
                "accepted terminal cannot use a preaccept-only category"
            )
        if projection.conclusion in {
            TerminalConclusion.OK,
            TerminalConclusion.NG,
        }:
            _require_current_bridge_result(event, decoded)
            _require_bridge_judgment_matches(projection.conclusion, decoded)
            return decoded
        if projection.conclusion is TerminalConclusion.NO_RESULT:
            if not operation_present or decoded is not None:
                raise ValueError(
                    "accepted NO_RESULT requires operation identity and no payload"
                )
            return None
        if projection.conclusion in {
            TerminalConclusion.ERROR,
            TerminalConclusion.MANUAL_STOPPED,
        }:
            if decoded is not None:
                raise ValueError("error terminal cannot contain result payload")
            return None
        raise ValueError("accepted terminal conclusion is invalid")

    if kind in {
        HistoryFactKind.REANALYSIS_STARTED,
        HistoryFactKind.REANALYSIS_CANCELLED,
    }:
        if expected_absent or not operation_present:
            raise ValueError(
                "reanalysis event requires revision and operation identity"
            )
        return decoded

    if kind is HistoryFactKind.REANALYSIS_COMPLETED:
        if expected_absent or not operation_present:
            raise ValueError(
                "reanalysis completion requires revision and operation identity"
            )
        if (
            decoded is not None
            and decoded.analysis_attempt_id == event.operation_id
        ):
            if (
                projection.fault_type is not None
                or projection.manual_judgment_revision is not None
                or projection.conclusion
                not in {TerminalConclusion.OK, TerminalConclusion.NG}
            ):
                raise ValueError(
                    "automatic reanalysis result has manual/incoherent fields"
                )
            _require_bridge_judgment_matches(projection.conclusion, decoded)
        elif (
            projection.conclusion in {
                TerminalConclusion.OK,
                TerminalConclusion.NG,
            }
            and decoded is None
            and projection.manual_judgment_revision is None
        ):
            raise ValueError(
                "reanalysis result without payload must preserve a manual result"
            )
        if projection.conclusion is TerminalConclusion.NO_RESULT and (
            decoded is not None
            or projection.fault_type is not None
            or projection.manual_judgment_revision is not None
        ):
            raise ValueError("automatic NO_RESULT has incoherent fields")
        return decoded

    if kind is HistoryFactKind.TERMINAL_RESULT_REPLACED:
        if expected_absent or operation_present:
            raise ValueError(
                "manual replacement requires revision and no operation identity"
            )
        if (
            projection.conclusion
            not in {TerminalConclusion.OK, TerminalConclusion.NG}
            or projection.error_category is not None
            or projection.reason
            or projection.manual_judgment_revision != event.proposed_revision
        ):
            raise ValueError("manual replacement projection is incoherent")
        if (
            projection.conclusion is TerminalConclusion.OK
            and projection.fault_type is not None
        ):
            raise ValueError("manual OK cannot contain fault_type")
        if projection.conclusion is TerminalConclusion.NG:
            if fault_types and projection.fault_type not in fault_types:
                raise ValueError(
                    "manual NG requires exact frozen fault-type membership"
                )
            if not fault_types and projection.fault_type is not None:
                raise ValueError(
                    "generic manual NG cannot contain fault_type"
                )
        return decoded

    raise ValueError("unknown HistoryFactKind")


def _require_active_projection_without_result(
    projection: ResultProjection,
    decoded: _DecodedBridgeResult | None,
) -> None:
    if (
        decoded is not None
        or projection.fault_type is not None
        or projection.manual_judgment_revision is not None
    ):
        raise ValueError("active projection contains terminal/manual fields")
    return None


def _require_current_bridge_result(
    event: ProjectedHistoryEvent,
    decoded: _DecodedBridgeResult | None,
) -> None:
    if (
        decoded is None
        or event.operation_id is None
        or decoded.analysis_attempt_id != event.operation_id
    ):
        raise ValueError(
            "automatic result payload must match the event operation identity"
        )


def _require_bridge_judgment_matches(
    conclusion: TerminalConclusion,
    decoded: _DecodedBridgeResult | None,
) -> None:
    if decoded is None:
        raise ValueError("automatic result requires bridge payload")
    expected = (
        "PASS" if conclusion is TerminalConclusion.OK else "FAIL"
    )
    if decoded.judgment != expected:
        raise ValueError("bridge result judgment contradicts conclusion")


def _decode_frozen_json(payload: object) -> object:
    return _decode_bridge_result(payload).value


def _decode_bridge_result(payload: object) -> _DecodedBridgeResult:
    if type(payload) is not FrozenPayload:
        raise ValueError("result payload must be an exact FrozenPayload")
    value_id = payload.value_id
    canonical = payload.canonical_bytes
    prefix = "bridge-result:"
    if not value_id.startswith(prefix):
        raise ValueError("result payload identity must use bridge-result")
    analysis_attempt_id = value_id[len(prefix):]
    require_uuid4(analysis_attempt_id, "bridge result attempt identity")
    try:
        decoded = json.loads(
            canonical.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("result payload is invalid canonical JSON") from exc
    if type(decoded) is not dict or set(decoded) != {"judgment", "result"}:
        raise ValueError("result payload root has an invalid shape")
    judgment = decoded["judgment"]
    if type(judgment) is not str or judgment not in {"PASS", "FAIL"}:
        raise ValueError("result payload judgment is invalid")
    canonical_prefix = f'{{"judgment":"{judgment}","result":'.encode("utf-8")
    if not canonical.startswith(canonical_prefix) or not canonical.endswith(b"}"):
        raise ValueError("result payload is noncanonical")
    return _DecodedBridgeResult(
        analysis_attempt_id=analysis_attempt_id,
        judgment=judgment,
        value=decoded,
    )


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")
