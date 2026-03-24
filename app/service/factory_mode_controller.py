from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QObject

from app.services_container import (
    FactoryControllerState,
    FactoryPlcIngressPayload,
    FactoryReviewPayload,
    FactoryRuntimeConfigPayload,
    FactoryRuntimeStartPayload,
    FactoryScanIngressPayload,
)


class FactoryModeController(QObject):
    """Owns the factory-mode page state and the UI-facing review contract."""

    __test__ = False

    def __init__(self, bus, services=None):
        super().__init__()
        self.bus = bus
        self.services = services
        self._state = FactoryControllerState()
        self._review_payload = FactoryReviewPayload()
        self._last_plc_ingress: FactoryPlcIngressPayload | None = None
        self._last_scan_ingress: FactoryScanIngressPayload | None = None
        self._connect_bus()

    @property
    def state(self) -> FactoryControllerState:
        return self._state

    @property
    def review_payload(self) -> FactoryReviewPayload:
        return self._review_payload

    @property
    def last_plc_ingress(self) -> FactoryPlcIngressPayload | None:
        return self._last_plc_ingress

    @property
    def last_scan_ingress(self) -> FactoryScanIngressPayload | None:
        return self._last_scan_ingress

    def set_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        manual_primary_config_id: int | None = None,
    ) -> FactoryControllerState:
        if self._has_review_context():
            self._reset_review_context("selection_changed")

        selected = tuple(selected_config_ids)
        is_ready = (
            len(selected) == 2
            and len(set(selected)) == 2
            and manual_primary_config_id in selected
        )
        self._state = FactoryControllerState(
            selected_config_ids=selected,
            manual_primary_config_id=manual_primary_config_id,
            active_config_id=None,
            displayed_config_id=None,
            run_phase="armed" if is_ready else "not_ready",
            trigger_source=None,
            is_ready=is_ready,
        )
        self._emit_state()
        return self._state

    def clear_display_state(self, reason: str) -> None:
        self._reset_review_context(reason)

    def handle_plc_event(self, payload: FactoryPlcIngressPayload) -> None:
        self._last_plc_ingress = payload

    def handle_scan_event(self, payload: FactoryScanIngressPayload) -> None:
        self._last_scan_ingress = payload

    def handle_runtime_start_accepted(self, payload: FactoryRuntimeStartPayload) -> None:
        if self._has_review_context():
            self._reset_review_context("next_run_accepted")

        self._state = replace(
            self._state,
            active_config_id=payload.config_id,
            displayed_config_id=payload.config_id,
            run_phase="running",
            trigger_source=payload.trigger_source,
        )
        self._emit_state()

    def handle_runtime_analyzing_entered(self, payload: FactoryRuntimeConfigPayload) -> None:
        self._state = replace(
            self._state,
            active_config_id=payload.config_id,
            displayed_config_id=payload.config_id,
            run_phase="analyzing",
        )
        self._emit_state()

    def handle_runtime_auto_analysis_succeeded(self, payload: FactoryReviewPayload) -> None:
        self._apply_review_payload(payload)

    def handle_runtime_auto_analysis_no_result(self, payload: FactoryReviewPayload) -> None:
        self._apply_review_payload(payload)

    def handle_runtime_auto_analysis_error(self, payload: FactoryReviewPayload) -> None:
        self._apply_review_payload(payload)

    def handle_runtime_reanalysis_completed(self, payload: FactoryReviewPayload) -> None:
        self._apply_review_payload(payload)

    def _connect_bus(self) -> None:
        self.bus.factory_plc_event.connect(self.handle_plc_event)
        self.bus.factory_scan_event.connect(self.handle_scan_event)
        self.bus.factory_runtime_start_accepted.connect(self.handle_runtime_start_accepted)
        self.bus.factory_runtime_analyzing_entered.connect(self.handle_runtime_analyzing_entered)
        self.bus.factory_runtime_auto_analysis_succeeded.connect(
            self.handle_runtime_auto_analysis_succeeded
        )
        self.bus.factory_runtime_auto_analysis_no_result.connect(
            self.handle_runtime_auto_analysis_no_result
        )
        self.bus.factory_runtime_auto_analysis_error.connect(
            self.handle_runtime_auto_analysis_error
        )
        self.bus.factory_runtime_reanalysis_completed.connect(
            self.handle_runtime_reanalysis_completed
        )

    def _apply_review_payload(self, payload: FactoryReviewPayload) -> None:
        trigger_source = payload.trigger_source
        if trigger_source is None:
            trigger_source = self._state.trigger_source

        self._review_payload = payload
        self._state = replace(
            self._state,
            active_config_id=None,
            displayed_config_id=payload.displayed_config_id,
            run_phase="reviewing",
            trigger_source=trigger_source,
        )
        self.bus.factory_review_changed.emit(payload)
        self._emit_state()

    def _has_review_context(self) -> bool:
        return (
            self._review_payload != FactoryReviewPayload()
            or self._state.displayed_config_id is not None
            or self._state.run_phase in {"reviewing", "reanalyzing"}
        )

    def _reset_review_context(self, reason: str) -> None:
        self._review_payload = FactoryReviewPayload()
        self._state = replace(
            self._state,
            active_config_id=None,
            displayed_config_id=None,
            run_phase="armed" if self._state.is_ready else "not_ready",
            trigger_source=None,
        )
        self.bus.factory_display_reset.emit(reason)
        self.bus.factory_review_changed.emit(self._review_payload)
        self._emit_state()

    def _emit_state(self) -> None:
        self.bus.factory_state_changed.emit(self._state)
