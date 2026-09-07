from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import fields, replace
from math import isfinite
from types import SimpleNamespace
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.service.factory_review_payloads import (
    FactoryReviewPayload,
    FactoryReviewWindow,
)
from app.service.result_dashboard_layout import (
    DEFAULT_RESULT_DASHBOARD_LAYOUT,
    PANEL_FAMILIES,
    PANEL_LOUDNESS,
    PANEL_OCTAVE,
    PANEL_SPECTRUM,
    PANEL_SPL,
    PANEL_STFT,
    PANEL_TITLES,
    PANEL_VIBRATION_FREQUENCY_STABILITY,
    PANEL_VIBRATION_OCTAVE,
    PANEL_VIBRATION_PULSE,
    PANEL_VIBRATION_SPECTRUM,
    PANEL_VIBRATION_STFT,
    normalize_result_dashboard_layout_for_display,
    resolve_layout_channel,
)
from app.service.result_history import (
    ActiveRunChanged,
    ActiveRunRemoved,
    ErrorDisplayPayload,
    ReanalysisCancelled,
    ReanalysisCompleted,
    ReanalysisStarted,
    ResultHistoryCardSnapshot,
    ResultHistoryError,
    ResultHistorySnapshot,
    ResultHistoryStore,
    ResultHistoryTransition,
    ResultPhase,
    ReviewDisplayPayload,
    RunTerminated,
    TerminalResultReplaced,
)
from app.service.result_history_contracts import (
    HistoryProjectionDiagnostic,
    HistoryProjectionDiagnosticCode,
    ManualJudgmentRequest,
    ManualJudgmentTarget,
    ReanalysisCancelRequest,
    ReanalysisRequest,
    ResultOperationAccepted,
    ResultOperationRejected,
    ResultOperationRejectionCode,
    ResultOperationResult,
)
from app.services_container import FactoryControllerState
from ui.modules.loudness import LoudnessModule
from ui.modules.octave import OctaveModule
from ui.modules.result_history import ResultHistoryModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.modules.stft import StftModule
from ui.modules.vibration import VibrationAxisModule, VibrationTrendModule
from ui.widgets.cards import Card


logger = logging.getLogger(__name__)


class _ProjectionBus(QObject):
    """Dashboard-private signal context used only by mounted chart widgets."""

    config_changed = pyqtSignal()
    factory_state_changed = pyqtSignal(object)
    factory_review_changed = pyqtSignal(object)
    factory_display_reset = pyqtSignal(str)
    factory_runtime_start_accepted = pyqtSignal(object)
    factory_runtime_analyzing_entered = pyqtSignal(object)
    stft_updated = pyqtSignal(int, str, int, object)
    fft_updated = pyqtSignal(int, str, int, object)
    spl_updated = pyqtSignal(int, str, int, object)
    loudness_updated = pyqtSignal(int, str, int, object)
    octave_updated = pyqtSignal(int, str, int, object)
    current_trace_updated = pyqtSignal(int, str, object)


class _FactoryDisplayProxy:
    def __init__(self, request_reanalysis: Callable[[float, float], object]):
        self.state = FactoryControllerState()
        self.review_payload = FactoryReviewPayload()
        self._request_reanalysis = request_reanalysis

    def begin_reanalysis(self, start_sec: float, end_sec: float):
        return self._request_reanalysis(start_sec, end_sec)


class _ServicesProxy:
    def __init__(
        self,
        source: object,
        bus: _ProjectionBus,
        factory_mode: _FactoryDisplayProxy,
    ):
        self._source = source
        self.bus = bus
        self.factory_mode = factory_mode
        self.config_service = _FrozenConfigService(
            getattr(source, "config_service", None)
        )

    def __getattr__(self, name: str):
        return getattr(self._source, name)


class _FrozenConfigService:
    def __init__(self, source: object):
        self._source = source
        self._config: dict[str, Any] = {}

    def set_config(self, config: dict[str, Any]) -> None:
        self._config = deepcopy(config)

    def reset(self) -> None:
        self._config = {}

    def load_config(self, _config_id):
        return deepcopy(self._config)

    def load_config_for_result_dashboard(self, _config_id):
        return deepcopy(self._config)

    def __getattr__(self, name: str):
        return getattr(self._source, name)


class _PanelFactory:
    def __init__(
        self,
        widget_type: type[QWidget],
        vibration_mode: bool | None = None,
    ):
        self.widget_type = widget_type
        self.vibration_mode = vibration_mode


_PANEL_FACTORIES = {
    PANEL_STFT: _PanelFactory(StftModule, False),
    PANEL_SPECTRUM: _PanelFactory(SpectrumModule, False),
    PANEL_OCTAVE: _PanelFactory(OctaveModule, False),
    PANEL_SPL: _PanelFactory(SplModule),
    PANEL_LOUDNESS: _PanelFactory(LoudnessModule),
    PANEL_VIBRATION_STFT: _PanelFactory(StftModule, True),
    PANEL_VIBRATION_SPECTRUM: _PanelFactory(SpectrumModule, True),
    PANEL_VIBRATION_OCTAVE: _PanelFactory(OctaveModule, True),
    PANEL_VIBRATION_FREQUENCY_STABILITY: _PanelFactory(VibrationAxisModule),
    PANEL_VIBRATION_PULSE: _PanelFactory(VibrationTrendModule),
}
_EVENT_TYPES = (
    ActiveRunChanged,
    RunTerminated,
    TerminalResultReplaced,
    ReanalysisStarted,
    ReanalysisCompleted,
    ReanalysisCancelled,
    ActiveRunRemoved,
)
_STFT_SCORE_METRIC_LABEL = "频谱最低得分："


class ResultDashboardModule(QWidget):
    module_id = "result_dashboard"

    manual_judgment_requested = pyqtSignal(object)
    manual_judgment_target_changed = pyqtSignal(object)
    reanalysis_requested = pyqtSignal(object)
    reanalysis_cancel_requested = pyqtSignal(object)
    history_diagnostic = pyqtSignal(object)
    preview_projected = pyqtSignal(str, object)
    decisionPainted = pyqtSignal(object, object)

    def __init__(
        self,
        services,
        result_history_store: ResultHistoryStore | None = None,
        manual_judgment_widget: QWidget | None = None,
    ):
        super().__init__()
        self.services = services
        self._history_store = (
            result_history_store
            if result_history_store is not None
            else ResultHistoryStore()
        )
        self._projected_snapshot: ResultHistorySnapshot | None = None
        self._projected_run_id: str | None = None
        self._preview_target_run_id: str | None = None
        self._manual_judgment_target: ManualJudgmentTarget | None = None
        self._history_projection_actionable = False
        self._display_config: dict[str, Any] = {}
        self._display_layout: dict[str, Any] | None = None
        self._display_layout_structure_signature: tuple | None = None
        self._cards_by_panel_id: dict[str, Card] = {}
        self._panel_widgets_by_id: dict[str, QWidget] = {}
        self._grid_positions_by_id: dict[str, tuple[int, int, int, int]] = {}
        self._row_count = 0
        self._column_count = 0
        self._source_bus_connections: list[tuple[object, object]] = []
        self._decision_binding_by_run: dict[str, tuple[str, str]] = {}
        self._decision_origin_revision_by_run: dict[str, int] = {}
        self._applied_enrichment_keys: set[tuple[str, str, str]] = set()
        self._enriched_payload_by_binding: dict[
            tuple[str, str, str], ReviewDisplayPayload
        ] = {}
        self._current_decision_key: tuple[str, str, str, int] | None = None
        self._last_acknowledged_decision_by_run: dict[
            str, tuple[str, str, str, int]
        ] = {}
        self._projection_refresh_scheduled = False
        self._projection_closed = False

        self._projection_bus = _ProjectionBus(self)
        self._factory_display = _FactoryDisplayProxy(self.request_reanalysis)
        self._projection_services = _ServicesProxy(
            services,
            self._projection_bus,
            self._factory_display,
        )

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._history_module = ResultHistoryModule(
            self,
            payload_provider=self._display_payload_for_run,
            config_provider=self._load_history_config,
            manual_judgment_widget=manual_judgment_widget,
        )
        self._history_module.run_selected.connect(self.select_run)
        self._history_module.follow_latest_requested.connect(
            self.set_follow_latest
        )
        self._history_module.decisionPainted.connect(
            self._on_decision_history_painted
        )
        root.addWidget(self._history_module)

        self._display_stack = QStackedWidget(self)
        self._display_stack.setObjectName("result_dashboard_display_stack")
        root.addWidget(self._display_stack, 1)

        self._waiting_page = QWidget()
        self._waiting_page.setObjectName("result_dashboard_waiting_page")
        waiting_layout = QVBoxLayout(self._waiting_page)
        self._waiting_label = QLabel("等待测试结果")
        self._waiting_label.setObjectName("result_dashboard_waiting_label")
        self._waiting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        waiting_layout.addWidget(self._waiting_label)
        self._display_stack.addWidget(self._waiting_page)

        self._review_page = QWidget()
        self._review_page.setObjectName("result_dashboard_review_page")
        review_layout = QVBoxLayout(self._review_page)
        review_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)
        self._scroll.setWidget(self._grid_widget)
        review_layout.addWidget(self._scroll)
        self._display_stack.addWidget(self._review_page)

        self._error_page = QWidget()
        self._error_page.setObjectName("result_dashboard_error_page")
        error_layout = QVBoxLayout(self._error_page)
        self._error_category = QLabel()
        self._error_category.setObjectName("result_dashboard_error_category")
        self._error_reason = QLabel()
        self._error_reason.setObjectName("result_dashboard_error_reason")
        self._error_reason.setWordWrap(True)
        self._error_context = QLabel()
        self._error_context.setObjectName("result_dashboard_error_context")
        self._error_context.setWordWrap(True)
        error_layout.addWidget(self._error_category)
        error_layout.addWidget(self._error_reason)
        error_layout.addWidget(self._error_context)
        error_layout.addStretch(1)
        self._display_stack.addWidget(self._error_page)

        self._connect_source_live_bus()
        self.refresh_history_projection()

    def cards_by_panel_id(self) -> dict[str, Card]:
        return dict(self._cards_by_panel_id)

    def panel_widget(self, panel_id: str) -> QWidget | None:
        return self._panel_widgets_by_id.get(panel_id)

    def grid_position(self, panel_id: str) -> tuple[int, int, int, int] | None:
        return self._grid_positions_by_id.get(panel_id)

    def display_stack(self) -> QStackedWidget:
        return self._display_stack

    def projection_bus(self) -> _ProjectionBus:
        return self._projection_bus

    def displayed_config(self) -> dict[str, Any]:
        return deepcopy(self._display_config)

    def projected_snapshot(self) -> ResultHistorySnapshot | None:
        return self._projected_snapshot

    def projected_run_id(self) -> str | None:
        return self._projected_run_id

    def preview_target_run_id(self) -> str | None:
        return self._preview_target_run_id

    def selected_run_id(self) -> str | None:
        return self._history_store.snapshot().selected_run_id

    def apply_history_event(self, event) -> ResultHistoryTransition:
        self._require_dashboard_thread()
        if type(event) not in _EVENT_TYPES:
            raise ResultHistoryError("dashboard accepts only exact history events")
        transition = self._history_store.apply(event)
        if type(event) is ReanalysisStarted:
            self._purge_staged_projection_run(event.run_id)
        self._project_committed_transition(transition)
        return transition

    def apply_enrichment_projection(self, event: object) -> bool:
        """Refresh matching terminal display data without changing history."""

        self._require_dashboard_thread()
        run_id = getattr(event, "run_id", None)
        attempt_id = getattr(event, "attempt_id", None)
        terminal_revision = getattr(event, "terminal_revision", None)
        expected_revision = getattr(event, "expected_revision", None)
        payload = getattr(event, "review_payload", None)
        if (
            type(run_id) is not str
            or type(attempt_id) is not str
            or type(terminal_revision) is not str
            or type(expected_revision) is not int
            or type(payload) is not ReviewDisplayPayload
        ):
            return False
        if self._projection_closed:
            return False
        snapshot = self._history_store.snapshot()
        target = next(
            (
                card
                for card in snapshot.terminal_cards
                if card.run_id == run_id
            ),
            None,
        )
        base_payload = (
            self._history_store.display_payload(run_id)
            if target is not None
            else None
        )
        identity = (
            self._projection_identity(base_payload.review, target)
            if type(base_payload) is ReviewDisplayPayload
            else None
        )
        if identity is not None:
            self._set_decision_binding(run_id, identity)
        binding = self._decision_binding_by_run.get(run_id)
        origin_revision = self._decision_origin_revision_by_run.get(run_id)
        key = (run_id, attempt_id, terminal_revision)
        exact_revision = (
            target is not None
            and target.revision == expected_revision
            and origin_revision == expected_revision
        )
        manual_only_revision = (
            target is not None
            and origin_revision is not None
            and target.manual_judgment_revision == target.revision
            and target.revision >= origin_revision
            and target.revision >= expected_revision
        )
        if (
            target is None
            or target.is_active_run
            or target.phase is not ResultPhase.TERMINAL
            or binding != (attempt_id, terminal_revision)
            or not (exact_revision or manual_only_revision)
            or key in self._applied_enrichment_keys
        ):
            return False
        self._applied_enrichment_keys.add(key)
        self._enriched_payload_by_binding[key] = payload
        overlay = self._display_payload_for_run(run_id)
        if type(overlay) is ReviewDisplayPayload:
            self._history_module.refresh_terminal_payload(run_id, overlay)
        if snapshot.selected_run_id == run_id:
            if type(overlay) is ReviewDisplayPayload:
                self._show_review(target, overlay)
        return True

    def project_history_transition(
        self,
        transition: ResultHistoryTransition,
    ) -> None:
        self._require_dashboard_thread()
        if type(transition) is not ResultHistoryTransition:
            raise ResultHistoryError(
                "dashboard accepts only exact history transitions"
            )
        self._project_committed_transition(transition)

    def history_snapshot(self) -> ResultHistorySnapshot:
        self._require_dashboard_thread()
        return self._history_store.snapshot()

    def select_run(self, run_id: str) -> ResultHistoryTransition:
        self._require_dashboard_thread()
        transition = self._history_store.select_run(run_id)
        self._project_committed_transition(transition)
        return transition

    def set_follow_latest(self, enabled: bool) -> ResultHistoryTransition:
        self._require_dashboard_thread()
        transition = self._history_store.set_follow_latest(enabled)
        self._project_committed_transition(transition)
        return transition

    def refresh_dashboard(self) -> None:
        self.refresh_history_projection()

    def refresh_history_projection(self) -> None:
        self._refresh_history_projection(diagnose_failure=True)

    def _refresh_history_projection(self, *, diagnose_failure: bool) -> None:
        self._require_dashboard_thread()
        snapshot = self._history_store.snapshot()
        payload = self._payload_for_snapshot(snapshot)
        after = self._history_store.snapshot()
        if after != snapshot:
            self._diagnose(
                HistoryProjectionDiagnosticCode.CONCURRENT_STORE_CHANGE,
                snapshot.selected_run_id,
                snapshot.revision,
                after.revision,
                "store changed while refreshing the selected payload",
            )
            snapshot = after
            payload = self._payload_for_snapshot(snapshot)
            final = self._history_store.snapshot()
            if final != snapshot:
                return
        try:
            self._project_snapshot(snapshot, payload)
        except Exception as exc:
            self._history_projection_actionable = False
            self._clear_manual_judgment_target()
            if diagnose_failure:
                self._diagnose_projection_failure(snapshot, exc)
            return
        self._history_projection_actionable = True
        self._publish_manual_judgment_target(snapshot)

    def _project_committed_transition(
        self,
        transition: ResultHistoryTransition,
    ) -> None:
        if transition.evicted_run_id is not None:
            self._purge_staged_projection_run(transition.evicted_run_id)
        self._history_projection_actionable = False
        try:
            self._project_coherent_transition(transition)
        except Exception as exc:
            self._clear_manual_judgment_target()
            self._diagnose_projection_failure(transition.snapshot, exc)
            self._schedule_projection_refresh_once()
            return
        self._history_projection_actionable = True
        self._publish_manual_judgment_target(self._history_store.snapshot())

    def _schedule_projection_refresh_once(self) -> None:
        if self._projection_closed or self._projection_refresh_scheduled:
            return
        self._projection_refresh_scheduled = True
        QTimer.singleShot(0, self._run_scheduled_projection_refresh)

    def _run_scheduled_projection_refresh(self) -> None:
        self._projection_refresh_scheduled = False
        if self._projection_closed:
            return
        self._refresh_history_projection(diagnose_failure=False)

    def _project_coherent_transition(
        self,
        transition: ResultHistoryTransition,
    ) -> None:
        current = self._history_store.snapshot()
        incoming = transition.snapshot
        if incoming.revision < current.revision:
            self._diagnose(
                HistoryProjectionDiagnosticCode.STALE_TRANSITION,
                incoming.selected_run_id,
                incoming.revision,
                current.revision,
                "transition is older than the injected store",
            )
            self.refresh_history_projection()
            return
        if incoming.revision > current.revision or incoming != current:
            self._diagnose(
                HistoryProjectionDiagnosticCode.FOREIGN_TRANSITION,
                incoming.selected_run_id,
                incoming.revision,
                current.revision,
                "transition does not match the injected store snapshot",
            )
            self.refresh_history_projection()
            return
        payload = self._payload_for_snapshot(incoming)
        after = self._history_store.snapshot()
        if after != incoming:
            self._diagnose(
                HistoryProjectionDiagnosticCode.CONCURRENT_STORE_CHANGE,
                incoming.selected_run_id,
                incoming.revision,
                after.revision,
                "store changed while copying the selected payload",
            )
            self.refresh_history_projection()
            return
        self._history_module.render_transition(transition)
        self._project_snapshot(incoming, payload, render_history=False)

    def _payload_for_snapshot(self, snapshot: ResultHistorySnapshot):
        run_id = snapshot.selected_run_id
        if run_id is None:
            return None
        if any(card.run_id == run_id for card in snapshot.active_cards):
            return None
        return self._display_payload_for_run(run_id)

    def _display_payload_for_run(self, run_id: str):
        base_payload = self._history_store.display_payload(run_id)
        snapshot = self._history_store.snapshot()
        selected = next(
            (
                card
                for card in snapshot.terminal_cards
                if card.run_id == run_id
            ),
            None,
        )
        binding = self._decision_binding_by_run.get(run_id)
        if (
            selected is None
            or selected.is_active_run
            or selected.phase is not ResultPhase.TERMINAL
            or binding is None
            or type(base_payload) is not ReviewDisplayPayload
        ):
            return base_payload
        origin_revision = self._decision_origin_revision_by_run.get(run_id)
        compatible_revision = (
            origin_revision is not None
            and (
                selected.revision == origin_revision
                or (
                    selected.manual_judgment_revision == selected.revision
                    and selected.revision >= origin_revision
                )
            )
        )
        if not compatible_revision:
            return base_payload
        attempt_id, terminal_revision = binding
        enriched = self._enriched_payload_by_binding.get(
            (run_id, attempt_id, terminal_revision)
        )
        if enriched is None:
            return base_payload
        return self._merge_enrichment_display_overlay(base_payload, enriched)

    @staticmethod
    def _merge_enrichment_display_overlay(
        current: ReviewDisplayPayload,
        enrichment: ReviewDisplayPayload,
    ) -> ReviewDisplayPayload:
        current_review = deepcopy(current.review)
        enrichment_review = enrichment.review
        if not isinstance(current_review, dict) or not isinstance(
            enrichment_review,
            dict,
        ):
            return current
        chart_payload = enrichment_review.get("chart_payload")
        summary_payload = enrichment_review.get("summary_payload")
        overlay_applied = False
        if isinstance(chart_payload, dict):
            current_review["chart_payload"] = deepcopy(chart_payload)
            overlay_applied = True
        current_summary = current_review.get("summary_payload")
        if isinstance(current_summary, dict) and isinstance(
            summary_payload,
            dict,
        ):
            enrichment_rows = summary_payload.get("rows")
            current_rows = current_summary.get("rows")
            if isinstance(enrichment_rows, list) and isinstance(
                current_rows,
                list,
            ):
                rows_by_key = {
                    row["item_key"]: deepcopy(row)
                    for row in current_rows
                    if isinstance(row, dict)
                    and isinstance(row.get("item_key"), str)
                    and row["item_key"].strip()
                }
                enrichment_rows_by_key = {
                    row["item_key"]: deepcopy(row)
                    for row in enrichment_rows
                    if isinstance(row, dict)
                    and isinstance(row.get("item_key"), str)
                    and row["item_key"].strip()
                    and row["item_key"] != "ai"
                }
                if enrichment_rows_by_key:
                    rows_by_key.update(enrichment_rows_by_key)
                    current_summary["rows"] = list(rows_by_key.values())
                    overlay_applied = True
        if not overlay_applied:
            return current
        return ReviewDisplayPayload(
            review=current_review,
            dashboard_config=deepcopy(current.dashboard_config),
        )

    def _project_snapshot(
        self,
        snapshot: ResultHistorySnapshot,
        payload,
        *,
        render_history: bool = True,
    ) -> None:
        retained_run_ids = {
            card.run_id
            for card in (*snapshot.active_cards, *snapshot.terminal_cards)
        }
        self._last_acknowledged_decision_by_run = {
            run_id: identity
            for run_id, identity in (
                self._last_acknowledged_decision_by_run.items()
            )
            if run_id in retained_run_ids
        }
        if render_history:
            self._history_module.render_snapshot(snapshot)
        self._projected_snapshot = snapshot
        self._update_preview_target(snapshot)
        selected = self._find_selected_card(snapshot)
        if selected is None and snapshot.active_cards:
            selected = snapshot.active_cards[0]
        if selected is None:
            self._show_context_dashboard(None)
            return
        if selected.is_active_run:
            self._show_context_dashboard(selected)
            return
        if payload is None:
            run_id = selected.run_id
            logger.error(
                "selected terminal history result has no payload: run_id=%s",
                run_id,
            )
            self._diagnose(
                HistoryProjectionDiagnosticCode.MISSING_SELECTED_PAYLOAD,
                run_id,
                snapshot.revision,
                snapshot.revision,
                "selected terminal result has no display payload",
            )
            self._show_error(
                ErrorDisplayPayload(
                    "history_projection",
                    "所选历史结果缺少显示数据",
                )
            )
            return
        if type(payload) is ReviewDisplayPayload:
            self._history_module.refresh_terminal_payload(
                selected.run_id,
                payload,
            )
            self._show_review(selected, payload)
            return
        if type(payload) is ErrorDisplayPayload:
            self._show_error(payload)
            return
        self._show_error(
            ErrorDisplayPayload(
                "history_projection",
                "所选历史结果包含无效显示数据",
            )
        )

    @staticmethod
    def _find_selected_card(
        snapshot: ResultHistorySnapshot,
    ) -> ResultHistoryCardSnapshot | None:
        run_id = snapshot.selected_run_id
        return next(
            (
                card
                for card in (*snapshot.active_cards, *snapshot.terminal_cards)
                if card.run_id == run_id
            ),
            None,
        )

    def _show_waiting(self) -> None:
        self._teardown_review_projection()
        self._display_stack.setCurrentWidget(self._waiting_page)

    def _show_context_dashboard(
        self,
        card: ResultHistoryCardSnapshot | None,
    ) -> None:
        config_id = (
            card.config.config_id
            if card is not None
            else self._preferred_idle_config_id()
        )
        if config_id is None:
            self._show_waiting()
            return
        try:
            loader = getattr(
                self.services.config_service,
                "load_config_for_result_dashboard",
                None,
            )
            if not callable(loader):
                loader = self.services.config_service.load_config
            display_config = self._normalize_display_config(loader(config_id))
        except Exception:
            self._show_waiting()
            return
        if not display_config:
            self._show_waiting()
            return

        layout = self._layout_for_display_config(display_config)
        signature = self._layout_structure_signature(layout)
        run_id = card.run_id if card is not None else None
        phase = (
            "running"
            if card is not None
            and card.phase in {ResultPhase.RECORDING, ResultPhase.WAITING_SAVE}
            else "analyzing"
            if card is not None
            else "idle"
        )
        selected_ids = self._prepared_selected_config_ids()
        if config_id not in selected_ids:
            selected_ids = (config_id, *selected_ids)
        state = FactoryControllerState(
            selected_config_ids=selected_ids,
            displayed_config_id=config_id,
            run_phase=phase,
            trigger_source=card.trigger_source if card is not None else None,
        )
        review = FactoryReviewPayload(
            displayed_config_id=config_id,
            config_payload=deepcopy(display_config),
            run_id=run_id,
            trigger_source=card.trigger_source if card is not None else None,
        )

        self._factory_display.state = state
        self._factory_display.review_payload = review
        self._display_config = display_config
        self._display_layout = deepcopy(layout)
        self._projected_run_id = run_id
        self._projection_services.config_service.set_config(display_config)
        if signature != self._display_layout_structure_signature:
            self._display_layout_structure_signature = signature
            self._rebuild_dashboard(layout)
        else:
            self._refresh_mounted_panel_context(layout)
        self._projection_bus.factory_state_changed.emit(state)
        self._projection_bus.factory_review_changed.emit(review)
        if run_id is not None:
            context = SimpleNamespace(config_id=config_id, run_id=run_id)
            self._projection_bus.factory_runtime_start_accepted.emit(context)
            if phase == "analyzing":
                self._projection_bus.factory_runtime_analyzing_entered.emit(
                    context
                )
        self._display_stack.setCurrentWidget(self._review_page)

    def _preferred_idle_config_id(self) -> int | None:
        selected = self._prepared_selected_config_ids()
        if not selected:
            return None
        state = getattr(self.services.factory_mode, "state", None)
        displayed = getattr(state, "displayed_config_id", None)
        if (
            isinstance(displayed, int)
            and not isinstance(displayed, bool)
            and displayed in selected
        ):
            return displayed
        return selected[0]

    def _prepared_selected_config_ids(self) -> tuple[int, ...]:
        state = getattr(self.services.factory_mode, "state", None)
        raw = getattr(state, "selected_config_ids", ())
        selected = tuple(
            item
            for item in raw
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        )
        if selected:
            return selected
        settings = getattr(self.services, "factory_selection_settings", None)
        load = getattr(settings, "load_selected_config_ids", None)
        if not callable(load):
            return ()
        try:
            raw = load()
        except Exception:
            return ()
        return tuple(
            item
            for item in raw
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        )

    def _show_error(self, payload: ErrorDisplayPayload) -> None:
        self._teardown_review_projection()
        self._error_category.setText(f"错误类别：{payload.error_category}")
        self._error_reason.setText(payload.reason)
        self._error_context.setText(
            "\n".join(f"{item.label}：{item.value}" for item in payload.context)
        )
        self._display_stack.setCurrentWidget(self._error_page)

    def _teardown_review_projection(self) -> None:
        self._factory_display.state = FactoryControllerState()
        self._factory_display.review_payload = FactoryReviewPayload()
        self._projection_services.config_service.reset()
        self._projected_run_id = None
        self._display_config = {}
        self._display_layout = None
        self._display_layout_structure_signature = None
        self._clear_grid()

    def closeEvent(self, event) -> None:
        self._projection_closed = True
        self._projection_refresh_scheduled = False
        self._history_projection_actionable = False
        self._clear_manual_judgment_target()
        self._teardown_review_projection()
        self._clear_staged_projection_state()
        self._disconnect_source_live_bus()
        self._history_module.clear_metric_view_state()
        self._disconnect_projection_bus()
        super().closeEvent(event)

    def _load_history_config(self, config_id: int) -> dict | None:
        config_service = getattr(self.services, "config_service", None)
        if config_service is None:
            return None
        loader = getattr(
            config_service, "load_config_for_result_dashboard", None
        )
        if not callable(loader):
            loader = getattr(config_service, "load_config", None)
        if not callable(loader):
            return None
        try:
            config = loader(config_id)
        except Exception:
            return None
        return config if isinstance(config, dict) else None

    def _connect_source_live_bus(self) -> None:
        source_bus = getattr(self.services, "bus", None)
        if source_bus is None:
            return
        for signal_name in (
            "stft_updated",
            "fft_updated",
            "spl_updated",
            "loudness_updated",
            "octave_updated",
            "current_trace_updated",
        ):
            source = getattr(source_bus, signal_name, None)
            target = getattr(self._projection_bus, signal_name)
            if source is None or not callable(getattr(source, "connect", None)):
                continue

            if signal_name in {"spl_updated", "loudness_updated"}:
                history_target = (
                    self._history_module.on_live_spl
                    if signal_name == "spl_updated"
                    else self._history_module.on_live_loudness
                )
                source.connect(history_target)
                self._source_bus_connections.append(
                    (source, history_target)
                )

            def forward(*args, _target=target):
                if len(args) < 2:
                    return
                run_id = args[1]
                if run_id != self._projected_run_id:
                    return
                snapshot = self._projected_snapshot
                active = (
                    next(
                        (
                            card
                            for card in snapshot.active_cards
                            if card.run_id == run_id
                        ),
                        None,
                    )
                    if snapshot is not None
                    else None
                )
                if active is None:
                    return
                _target.emit(*args)

            source.connect(forward)
            self._source_bus_connections.append((source, forward))

    def _disconnect_source_live_bus(self) -> None:
        for source, callback in self._source_bus_connections:
            disconnect = getattr(source, "disconnect", None)
            if not callable(disconnect):
                continue
            try:
                disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        self._source_bus_connections.clear()

    def _disconnect_projection_bus(self) -> None:
        for signal_name in (
            "config_changed",
            "factory_state_changed",
            "factory_review_changed",
            "factory_display_reset",
            "factory_runtime_start_accepted",
            "factory_runtime_analyzing_entered",
            "stft_updated",
            "fft_updated",
            "spl_updated",
            "loudness_updated",
            "octave_updated",
            "current_trace_updated",
        ):
            try:
                getattr(self._projection_bus, signal_name).disconnect()
            except TypeError:
                pass

    def _show_review(
        self,
        card: ResultHistoryCardSnapshot,
        payload: ReviewDisplayPayload,
    ) -> None:
        identity = self._projection_identity(payload.review, card)
        decision_to_project = None
        if identity is not None:
            attempt_id, terminal_revision, history_revision = identity
            self._set_decision_binding(card.run_id, identity)
            decision_key = (
                card.run_id,
                attempt_id,
                terminal_revision,
                history_revision,
            )
            self._current_decision_key = decision_key
            if (
                decision_key
                != self._last_acknowledged_decision_by_run.get(card.run_id)
            ):
                decision_to_project = decision_key
        display_config = self._normalize_display_config(payload.dashboard_config)
        layout = self._layout_for_display_config(display_config)
        signature = self._layout_structure_signature(layout)
        review = self._normalize_review(payload.review, card)
        state = FactoryControllerState(
            selected_config_ids=(
                (card.config.config_id,)
                if card.config.config_id is not None
                else ()
            ),
            displayed_config_id=card.config.config_id,
            run_phase="reviewing",
            trigger_source=card.trigger_source,
        )

        self._factory_display.state = state
        self._factory_display.review_payload = review
        self._display_config = display_config
        self._display_layout = deepcopy(layout)
        self._projected_run_id = card.run_id
        self._projection_services.config_service.set_config(display_config)
        if signature != self._display_layout_structure_signature:
            self._display_layout_structure_signature = signature
            self._rebuild_dashboard(layout)
        else:
            self._refresh_mounted_panel_context(layout)
        self._projection_bus.factory_state_changed.emit(state)
        self._projection_bus.factory_review_changed.emit(review)
        self._display_stack.setCurrentWidget(self._review_page)
        if decision_to_project is not None:
            self._history_module.project_decision_revision(
                decision_to_project[0],
                decision_to_project[1],
                decision_to_project[2],
                decision_to_project[3],
            )
        self.update()

    def _set_decision_binding(
        self,
        run_id: str,
        identity: tuple[str, str, int],
    ) -> None:
        immutable_identity = identity[:2]
        previous_identity = self._decision_binding_by_run.get(run_id)
        if previous_identity == immutable_identity:
            return
        self._decision_binding_by_run[run_id] = immutable_identity
        self._decision_origin_revision_by_run[run_id] = identity[2]
        self._applied_enrichment_keys = {
            key for key in self._applied_enrichment_keys if key[0] != run_id
        }
        self._enriched_payload_by_binding = {
            key: value
            for key, value in self._enriched_payload_by_binding.items()
            if key[0] != run_id
        }

    def _purge_staged_projection_run(self, run_id: str) -> None:
        self._decision_binding_by_run.pop(run_id, None)
        self._decision_origin_revision_by_run.pop(run_id, None)
        self._applied_enrichment_keys = {
            key for key in self._applied_enrichment_keys if key[0] != run_id
        }
        self._enriched_payload_by_binding = {
            key: value
            for key, value in self._enriched_payload_by_binding.items()
            if key[0] != run_id
        }
        if self._current_decision_key is not None:
            if self._current_decision_key[0] == run_id:
                self._current_decision_key = None
        self._last_acknowledged_decision_by_run.pop(run_id, None)

    def _clear_staged_projection_state(self) -> None:
        self._decision_binding_by_run.clear()
        self._decision_origin_revision_by_run.clear()
        self._applied_enrichment_keys.clear()
        self._enriched_payload_by_binding.clear()
        self._current_decision_key = None
        self._last_acknowledged_decision_by_run.clear()

    def _on_decision_history_painted(
        self,
        revision: object,
        painted_at: object,
    ) -> None:
        current = self._current_decision_key
        if (
            type(revision) is not tuple
            or len(revision) != 4
            or current is None
            or revision != current
            or current
            == self._last_acknowledged_decision_by_run.get(current[0])
            or self._projected_run_id != current[0]
        ):
            return
        binding = self._decision_binding_by_run.get(current[0])
        if (
            binding != current[1:3]
            or self._decision_origin_revision_by_run.get(current[0])
            != current[3]
        ):
            return
        selected = self._find_selected_card(self._history_store.snapshot())
        if (
            selected is None
            or selected.run_id != current[0]
            or selected.is_active_run
            or selected.phase is not ResultPhase.TERMINAL
            or selected.revision != current[3]
        ):
            return
        self._last_acknowledged_decision_by_run[current[0]] = current
        self.decisionPainted.emit(current[2], painted_at)

    @staticmethod
    def _projection_identity(
        review: object,
        card: ResultHistoryCardSnapshot,
    ) -> tuple[str, str, int] | None:
        if type(review) is not dict:
            return None
        value = review.get("_projection_identity")
        if type(value) is not dict:
            return None
        attempt_id = value.get("attempt_id")
        terminal_revision = value.get("terminal_revision")
        history_revision = value.get("history_revision")
        if (
            type(attempt_id) is not str
            or type(terminal_revision) is not str
            or type(history_revision) is not int
            or history_revision != card.revision
        ):
            return None
        return attempt_id, terminal_revision, history_revision

    @staticmethod
    def _normalize_display_config(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return deepcopy(value)

    @staticmethod
    def _normalize_review(
        value: object,
        card: ResultHistoryCardSnapshot,
    ) -> FactoryReviewPayload:
        if isinstance(value, FactoryReviewPayload):
            review = value
        else:
            names = {item.name for item in fields(FactoryReviewPayload)}
            if isinstance(value, dict):
                values = {key: val for key, val in value.items() if key in names}
            else:
                values = {
                    name: getattr(value, name)
                    for name in names
                    if hasattr(value, name)
                }
            for key in ("default_window", "fallback_window"):
                window = values.get(key)
                if isinstance(window, dict):
                    start = window.get("start_sec")
                    end = window.get("end_sec")
                    if (
                        isinstance(start, (int, float))
                        and not isinstance(start, bool)
                        and isinstance(end, (int, float))
                        and not isinstance(end, bool)
                    ):
                        values[key] = FactoryReviewWindow(
                            float(start),
                            float(end),
                        )
            chart_payload = values.get("chart_payload")
            if isinstance(chart_payload, dict):
                normalized_charts = {}
                for channel, channel_payload in chart_payload.items():
                    try:
                        normalized_channel = int(channel)
                    except (TypeError, ValueError):
                        continue
                    normalized_charts[normalized_channel] = channel_payload
                values["chart_payload"] = normalized_charts
            labels = values.get("display_channel_labels")
            if isinstance(labels, dict):
                values["display_channel_labels"] = {
                    int(channel): label
                    for channel, label in labels.items()
                    if str(channel).lstrip("-").isdigit()
                }
            review = FactoryReviewPayload(**values)
        return replace(
            review,
            displayed_config_id=card.config.config_id,
            run_id=card.run_id,
            trigger_source=card.trigger_source,
            manual_reanalysis_allowed=card.manual_reanalysis_allowed,
        )

    def _layout_for_display_config(
        self,
        display_config: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(display_config, dict):
            return deepcopy(DEFAULT_RESULT_DASHBOARD_LAYOUT)
        return normalize_result_dashboard_layout_for_display(
            display_config.get("result_dashboard_layout"),
            selected_channels=self._normalize_channels(
                display_config.get("selected_channels", [])
            ),
        )

    def _rebuild_dashboard(self, layout: dict[str, Any]) -> None:
        self._clear_grid()
        self._apply_grid_stretch(
            rows=self._coerce_positive_int(layout.get("rows"), default=1),
            columns=self._coerce_positive_int(
                layout.get("columns"),
                default=1,
            ),
        )
        panels = layout.get("panels")
        if not isinstance(panels, list):
            return
        for panel in panels:
            if isinstance(panel, dict):
                self._add_panel_card(panel)

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._cards_by_panel_id.clear()
        self._panel_widgets_by_id.clear()
        self._grid_positions_by_id.clear()

    def _refresh_mounted_panel_context(self, layout: dict[str, Any]) -> None:
        panels = layout.get("panels")
        if not isinstance(panels, list):
            return
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            panel_id = panel.get("id")
            if type(panel_id) is not str:
                continue
            panel_widget = self._panel_widgets_by_id.get(panel_id)
            if panel_widget is not None:
                self._configure_panel_widget(panel_widget, panel)

    def _apply_grid_stretch(self, *, rows: int, columns: int) -> None:
        for row in range(max(self._row_count, rows)):
            self._grid.setRowStretch(row, 1 if row < rows else 0)
        for column in range(max(self._column_count, columns)):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        self._row_count = rows
        self._column_count = columns

    def _add_panel_card(self, panel: dict[str, Any]) -> None:
        panel_id = panel.get("id")
        panel_type = panel.get("type")
        if type(panel_id) is not str or type(panel_type) is not str:
            return
        factory = _PANEL_FACTORIES.get(panel_type)
        title = PANEL_TITLES.get(panel_type)
        position = self._panel_grid_position(panel)
        if factory is None or title is None or position is None:
            return
        panel_widget = factory.widget_type(self._projection_services)
        if factory.vibration_mode is not None:
            self._set_vibration_mode(panel_widget, factory.vibration_mode)
        self._configure_panel_widget(panel_widget, panel)
        self._enable_compact_chart_mode(panel_widget)
        card = Card(title, panel_widget)
        card.set_compact_dashboard_mode(True)
        self._bind_stft_score_metric(panel_type, panel_widget, card)
        row, column, row_span, column_span = position
        self._grid.addWidget(card, row, column, row_span, column_span)
        self._cards_by_panel_id[panel_id] = card
        self._panel_widgets_by_id[panel_id] = panel_widget
        self._grid_positions_by_id[panel_id] = position

    def _configure_panel_widget(
        self,
        panel_widget: QWidget,
        panel: dict[str, Any],
    ) -> None:
        self._bind_panel_channel(panel_widget, panel)
        if isinstance(panel_widget, StftModule):
            active_composition = getattr(
                self.services,
                "final_composition",
                None,
            )
            panel_widget.run_scoped_playback = getattr(
                active_composition,
                "playback",
                None,
            )
            panel_widget.selected_run_id_provider = self.selected_run_id
        refresh = getattr(
            panel_widget,
            "refresh_selected_display_channel",
            None,
        )
        if callable(refresh):
            refresh()

    def _enable_compact_chart_mode(self, panel_widget: QWidget) -> None:
        self._set_compact_display_mode(panel_widget)
        self._set_compact_display_mode(getattr(panel_widget, "plot", None))
        canvas = getattr(panel_widget, "canvas", None)
        self._set_compact_display_mode(getattr(canvas, "heatmap", None))

    @staticmethod
    def _set_compact_display_mode(target) -> None:
        method = getattr(target, "set_compact_display_mode", None)
        if callable(method):
            method(True)

    def _bind_stft_score_metric(
        self,
        panel_type: str,
        panel_widget: QWidget,
        card: Card,
    ) -> None:
        if panel_type not in {PANEL_STFT, PANEL_VIBRATION_STFT}:
            return
        current = getattr(panel_widget, "current_score_title", None)
        if callable(current):
            self._set_stft_card_score_metric(card, current())
        changed = getattr(panel_widget, "score_title_changed", None)
        if hasattr(changed, "connect"):
            changed.connect(
                lambda text, dashboard_card=card: (
                    self._set_stft_card_score_metric(dashboard_card, text)
                )
            )

    @staticmethod
    def _set_stft_card_score_metric(card: Card, text: str) -> None:
        card.set_title_metric(
            _STFT_SCORE_METRIC_LABEL,
            ResultDashboardModule._stft_score_value(text),
        )

    @staticmethod
    def _stft_score_value(text: str) -> str:
        normalized = str(text or "").strip()
        for separator in ("：", ":"):
            if separator in normalized:
                return normalized.split(separator, 1)[1].strip() or "--"
        return normalized or "--"

    def _bind_panel_channel(
        self,
        panel_widget: QWidget,
        panel: dict[str, Any],
    ) -> None:
        provider = self._provider_for_panel(panel)
        if hasattr(panel_widget, "selected_display_channel_provider"):
            panel_widget.selected_display_channel_provider = provider
        if hasattr(panel_widget, "selected_display_channel_sync"):
            panel_widget.selected_display_channel_sync = None
        panel_widget.strict_selected_display_channel = True

    def _provider_for_panel(
        self,
        panel: dict[str, Any],
    ) -> Callable[[], int | None]:
        channel_key = str(panel.get("channel") or "")
        panel_family = PANEL_FAMILIES.get(str(panel.get("type") or ""), "")

        def provider() -> int | None:
            return resolve_layout_channel(
                channel_key,
                selected_channels=self._selected_channels(),
                vibration_view_channel_index=self._vibration_channel(),
                panel_family=panel_family,
            )

        return provider

    def _selected_channels(self) -> list[int]:
        return self._normalize_channels(
            self._display_config.get("selected_channels", [])
        )

    def _vibration_channel(self) -> int | None:
        value = self._display_config.get("vibration_view_channel_index")
        if type(value) is not int:
            return None
        return value if value in self._selected_channels() else None

    @staticmethod
    def _normalize_channels(channels) -> list[int]:
        normalized = []
        for channel in channels or ():
            if type(channel) is not int:
                try:
                    if type(channel) is bool:
                        continue
                    channel = int(channel)
                except (TypeError, ValueError):
                    continue
            if channel not in normalized:
                normalized.append(channel)
        return normalized

    @staticmethod
    def _set_vibration_mode(
        panel_widget: QWidget,
        enabled: bool,
    ) -> None:
        method = getattr(panel_widget, "set_vibration_mode", None)
        if callable(method):
            method(enabled)

    @classmethod
    def _layout_structure_signature(
        cls,
        layout: dict[str, Any],
    ) -> tuple:
        panels = []
        for panel in layout.get("panels") or ():
            if not isinstance(panel, dict):
                continue
            panels.append(
                (
                    panel.get("id"),
                    panel.get("type"),
                    cls._coerce_non_negative_int(panel.get("row")),
                    cls._coerce_non_negative_int(panel.get("column")),
                    cls._coerce_positive_int(
                        panel.get("row_span"),
                        default=1,
                    ),
                    cls._coerce_positive_int(
                        panel.get("column_span"),
                        default=1,
                    ),
                )
            )
        return (
            cls._coerce_positive_int(layout.get("rows"), default=1),
            cls._coerce_positive_int(layout.get("columns"), default=1),
            tuple(sorted(panels)),
        )

    @classmethod
    def _panel_grid_position(
        cls,
        panel: dict[str, Any],
    ) -> tuple[int, int, int, int] | None:
        row = cls._coerce_non_negative_int(panel.get("row"))
        column = cls._coerce_non_negative_int(panel.get("column"))
        if row is None or column is None:
            return None
        return (
            row,
            column,
            cls._coerce_positive_int(panel.get("row_span"), default=1),
            cls._coerce_positive_int(panel.get("column_span"), default=1),
        )

    @staticmethod
    def _coerce_non_negative_int(value) -> int | None:
        if type(value) is bool:
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized >= 0 else None

    @staticmethod
    def _coerce_positive_int(value, *, default: int) -> int:
        if type(value) is bool:
            return default
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return default
        return normalized if normalized > 0 else default

    def request_manual_judgment(
        self,
        label: str,
        fault_type: str | None = None,
    ) -> ResultOperationResult:
        self._require_dashboard_thread()
        if type(label) is not str or label not in {"OK", "NG"}:
            return self._reject_operation(
                ResultOperationRejectionCode.INVALID_ARGUMENT,
                "人工判定标签不能为空",
            )
        if not self._history_projection_actionable:
            return self._reject_operation(
                ResultOperationRejectionCode.OPERATION_NOT_ALLOWED,
                "result projection is not actionable",
            )
        card, rejection = self._ordinary_selected_terminal(
            capability="manual_judgment_allowed"
        )
        if rejection is not None:
            return rejection
        assert card is not None
        if fault_type is not None and (
            type(fault_type) is not str or not fault_type.strip()
        ):
            return self._reject_operation(
                ResultOperationRejectionCode.INVALID_ARGUMENT,
                "fault type must be a non-empty string or None",
            )
        if (
            (label == "OK" and fault_type is not None)
            or (
                label == "NG"
                and card.config.fault_types
                and fault_type not in card.config.fault_types
            )
            or (
                label == "NG"
                and not card.config.fault_types
                and fault_type is not None
            )
        ):
            return self._reject_operation(
                ResultOperationRejectionCode.INVALID_ARGUMENT,
                "fault type does not match the frozen selected result",
            )
        request = ManualJudgmentRequest(
            card.run_id,
            label,
            card.revision,
            fault_type,
        )
        result = ResultOperationAccepted(request)
        self._history_module.preview_manual_judgment(
            card.run_id,
            label,
            fault_type,
        )
        self.manual_judgment_requested.emit(request)
        return result

    def request_reanalysis(
        self,
        start_sec: float,
        end_sec: float,
    ) -> ResultOperationResult:
        self._require_dashboard_thread()
        if (
            type(start_sec) not in (int, float)
            or type(end_sec) not in (int, float)
            or type(start_sec) is bool
            or type(end_sec) is bool
        ):
            return self._reject_operation(
                ResultOperationRejectionCode.INVALID_ARGUMENT,
                "重新分析时间窗口参数无效",
            )
        start = float(start_sec)
        end = float(end_sec)
        if (
            not isfinite(start)
            or not isfinite(end)
            or start < 0.0
            or start >= end
        ):
            return self._reject_operation(
                ResultOperationRejectionCode.INVALID_ARGUMENT,
                "重新分析时间窗口必须满足 0 <= 开始时间 < 结束时间",
            )
        if not self._history_projection_actionable:
            return self._reject_operation(
                ResultOperationRejectionCode.OPERATION_NOT_ALLOWED,
                "result projection is not actionable",
            )
        card, rejection = self._ordinary_selected_terminal(
            capability="manual_reanalysis_allowed"
        )
        if rejection is not None:
            return rejection
        request = ReanalysisRequest(
            card.run_id,
            card.revision,
            start,
            end,
        )
        result = ResultOperationAccepted(request)
        self.reanalysis_requested.emit(request)
        return result

    def request_reanalysis_cancel(self) -> ResultOperationResult:
        self._require_dashboard_thread()
        if not self._history_projection_actionable:
            return self._reject_operation(
                ResultOperationRejectionCode.OPERATION_NOT_ALLOWED,
                "result projection is not actionable",
            )
        snapshot = self._history_store.snapshot()
        selected = self._find_selected_card(snapshot)
        if selected is None:
            return self._reject_operation(
                ResultOperationRejectionCode.NO_SELECTION,
                "当前没有选中的结果",
            )
        if selected.is_active_run:
            return self._reject_operation(
                ResultOperationRejectionCode.TARGET_NOT_TERMINAL,
                "当前选中项尚未形成终止结果",
            )
        if selected.phase is not ResultPhase.REANALYZING:
            return self._reject_operation(
                ResultOperationRejectionCode.NOT_REANALYZING,
                "当前选中结果不在重新分析中",
            )
        if selected.operation_id is None:
            return self._reject_operation(
                ResultOperationRejectionCode.MISSING_OPERATION_ID,
                "当前重新分析缺少操作标识",
            )
        request = ReanalysisCancelRequest(
            selected.run_id,
            selected.operation_id,
            selected.revision,
        )
        result = ResultOperationAccepted(request)
        self.reanalysis_cancel_requested.emit(request)
        return result

    def _ordinary_selected_terminal(
        self,
        *,
        capability: str,
    ) -> tuple[
        ResultHistoryCardSnapshot | None,
        ResultOperationRejected | None,
    ]:
        snapshot = self._history_store.snapshot()
        selected = self._find_selected_card(snapshot)
        if selected is None:
            return None, self._reject_operation(
                ResultOperationRejectionCode.NO_SELECTION,
                "当前没有选中的结果",
            )
        if selected.is_active_run:
            return None, self._reject_operation(
                ResultOperationRejectionCode.TARGET_NOT_TERMINAL,
                "当前选中项尚未形成终止结果",
            )
        if selected.phase is not ResultPhase.TERMINAL:
            return None, self._reject_operation(
                ResultOperationRejectionCode.OPERATION_NOT_ALLOWED,
                "当前结果状态不允许该操作",
            )
        if not getattr(selected, capability):
            return None, self._reject_operation(
                ResultOperationRejectionCode.OPERATION_NOT_ALLOWED,
                "当前结果不允许该操作",
            )
        return selected, None

    @staticmethod
    def _reject_operation(
        code: ResultOperationRejectionCode,
        reason: str,
    ) -> ResultOperationRejected:
        return ResultOperationRejected(code, reason)

    def project_preview(self, run_id: str, payload: object) -> bool:
        self._require_dashboard_thread()
        if (
            type(run_id) is not str
            or run_id != self._preview_target_run_id
        ):
            return False
        self.preview_projected.emit(run_id, payload)
        return True

    def _update_preview_target(
        self,
        snapshot: ResultHistorySnapshot,
    ) -> None:
        selected = self._find_selected_card(snapshot)
        self._preview_target_run_id = (
            selected.run_id
            if (
                not snapshot.follow_latest
                and selected is not None
                and selected.is_active_run
            )
            else None
        )

    def _publish_manual_judgment_target(
        self,
        snapshot: ResultHistorySnapshot,
    ) -> None:
        selected = self._find_selected_card(snapshot)
        target = (
            ManualJudgmentTarget(
                run_id=selected.run_id,
                revision=selected.revision,
                fault_types=selected.config.fault_types,
                manual_judgment_allowed=selected.manual_judgment_allowed,
            )
            if (
                selected is not None
                and not selected.is_active_run
                and selected.config.manual_judgment_buttons_visible
            )
            else None
        )
        if target == self._manual_judgment_target:
            return
        self._manual_judgment_target = target
        self.manual_judgment_target_changed.emit(target)

    def _clear_manual_judgment_target(self) -> None:
        if self._manual_judgment_target is None:
            return
        self._manual_judgment_target = None
        self.manual_judgment_target_changed.emit(None)

    def _diagnose_projection_failure(
        self,
        snapshot: ResultHistorySnapshot,
        error: Exception,
    ) -> None:
        detail = str(error).strip() or type(error).__name__
        self._diagnose(
            HistoryProjectionDiagnosticCode.PROJECTION_FAILED,
            snapshot.selected_run_id,
            snapshot.revision,
            snapshot.revision,
            detail[:240],
        )

    def _require_dashboard_thread(self) -> None:
        if QThread.currentThread() is self.thread():
            return
        self._diagnose(
            HistoryProjectionDiagnosticCode.WRONG_QT_THREAD,
            None,
            None,
            None,
            "dashboard history entrypoint called from the wrong Qt thread",
        )
        raise RuntimeError(
            "dashboard history entrypoints require the owning Qt thread"
        )

    def _diagnose(
        self,
        code: HistoryProjectionDiagnosticCode,
        run_id: str | None,
        transition_revision: int | None,
        current_revision: int | None,
        detail: str,
    ) -> None:
        diagnostic = HistoryProjectionDiagnostic(
            code,
            run_id,
            transition_revision,
            current_revision,
            detail,
        )
        self.history_diagnostic.emit(diagnostic)
