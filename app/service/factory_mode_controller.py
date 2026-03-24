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
    _BLOCKED_START_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _ACTIVE_RUN_PHASES = frozenset({"running", "analyzing"})

    def __init__(self, bus, services=None):
        super().__init__()
        self.bus = bus
        self.services = services
        self._state = FactoryControllerState()
        self._review_payload = FactoryReviewPayload()
        self._pending_start: FactoryRuntimeStartPayload | None = None
        self._owned_runtime_config_id: int | None = None
        self._owned_reanalysis_config_id: int | None = None
        self._last_plc_ingress: FactoryPlcIngressPayload | None = None
        self._last_scan_ingress: FactoryScanIngressPayload | None = None
        self._selected_configs: dict[int, dict] = {}
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
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            return self._state

        selected = tuple(selected_config_ids)
        has_review_context = self._has_review_context()
        selection_changed = (
            selected != self._state.selected_config_ids
            or manual_primary_config_id != self._state.manual_primary_config_id
        )
        if has_review_context and not selection_changed:
            return self._state

        is_ready = False
        self._selected_configs = {}
        if len(selected) == 2 and len(set(selected)) == 2 and manual_primary_config_id in selected:
            self._selected_configs = self._load_selected_configs(selected)
            is_ready = (
                len(self._selected_configs) == len(selected)
                and not self._selected_configs_have_runtime_errors()
            )

        next_state = FactoryControllerState(
            selected_config_ids=selected,
            manual_primary_config_id=manual_primary_config_id,
            active_config_id=None,
            displayed_config_id=None,
            run_phase="armed" if is_ready else "not_ready",
            trigger_source=None,
            is_ready=is_ready,
        )

        if has_review_context:
            self._reset_review_context("selection_changed", state=next_state)
        else:
            self._state = next_state
            self._emit_state()
        return self._state

    def can_arm(self) -> bool:
        return self._state.is_ready and self._state.run_phase not in self._BLOCKED_START_PHASES

    def start_manual(self, workorder: dict | None = None) -> bool:
        config_id = self._state.manual_primary_config_id
        if config_id is None:
            return False
        return self._accept_start(config_id, "manual", workorder)

    def clear_display_state(self, reason: str) -> None:
        self.clear_for_next_run(reason)

    def handle_plc_event(self, payload: FactoryPlcIngressPayload) -> bool:
        self._last_plc_ingress = payload
        config_id = self._resolve_plc_config_id(payload.plc_device_key)
        if config_id is None:
            return False
        return self._accept_start(config_id, "plc")

    def handle_scan_event(self, payload: FactoryScanIngressPayload) -> bool:
        self._last_scan_ingress = payload
        config_id = self._resolve_scan_config_id()
        if config_id is None:
            return False
        return self._accept_start(config_id, "scan")

    def handle_runtime_started(self, payload: FactoryRuntimeStartPayload) -> None:
        if not self._matches_pending_start(payload):
            return

        self._pending_start = None
        if self._has_review_context():
            self._reset_review_context("next_run_accepted")

        self._owned_runtime_config_id = payload.config_id
        self._state = replace(
            self._state,
            active_config_id=payload.config_id,
            displayed_config_id=payload.config_id,
            run_phase="running",
            trigger_source=payload.trigger_source,
        )
        self._emit_state()

    def handle_runtime_start_accepted(self, payload: FactoryRuntimeStartPayload) -> None:
        self.handle_runtime_started(payload)

    def handle_runtime_analyzing(self, payload: FactoryRuntimeConfigPayload) -> None:
        if not self._owns_active_runtime_config(payload.config_id):
            return

        self._state = replace(
            self._state,
            active_config_id=payload.config_id,
            displayed_config_id=payload.config_id,
            run_phase="analyzing",
        )
        self._emit_state()

    def handle_runtime_analyzing_entered(self, payload: FactoryRuntimeConfigPayload) -> None:
        self.handle_runtime_analyzing(payload)

    def handle_runtime_review_ready(self, payload: FactoryReviewPayload) -> None:
        if not self._owns_active_review_payload(payload):
            return

        self._owned_runtime_config_id = None
        self._apply_review_payload(payload)

    def handle_runtime_auto_analysis_succeeded(self, payload: FactoryReviewPayload) -> None:
        self.handle_runtime_review_ready(payload)

    def handle_runtime_no_result(self, payload: FactoryReviewPayload) -> None:
        self._apply_review_payload(payload)

    def handle_runtime_auto_analysis_no_result(self, payload: FactoryReviewPayload) -> None:
        self.handle_runtime_no_result(payload)

    def handle_runtime_auto_analysis_error(self, payload: FactoryReviewPayload) -> None:
        self.handle_runtime_no_result(payload)

    def handle_runtime_aborted(self, payload) -> None:
        if not self._owns_active_runtime_config(payload.config_id):
            return
        self._reset_review_context("runtime_aborted")

    def begin_reanalysis(self, start_sec: float, end_sec: float) -> bool:
        if (
            self._state.run_phase != "reviewing"
            or self._state.displayed_config_id is None
            or not self._review_payload.manual_reanalysis_allowed
        ):
            return False

        config_id = self._state.displayed_config_id
        self._owned_reanalysis_config_id = config_id
        self._review_payload = replace(
            self._review_payload,
            displayed_config_id=config_id,
            trigger_source=self._state.trigger_source,
            no_result=False,
            error_message=None,
            judgment_payload=None,
        )
        self._state = replace(
            self._state,
            active_config_id=None,
            displayed_config_id=config_id,
            run_phase="reanalyzing",
        )
        self.bus.factory_review_changed.emit(self._review_payload)
        self._emit_state()
        test_manager = getattr(self.services, "test", None)
        if test_manager is not None:
            test_manager.rejudge(config_id, start_sec, end_sec)
        return True

    def finish_reanalysis_success(self, payload: FactoryReviewPayload) -> None:
        self._owned_reanalysis_config_id = None
        self._apply_review_payload(payload)

    def finish_reanalysis_failure(self, error: Exception | str) -> None:
        self._owned_reanalysis_config_id = None
        self._apply_review_payload(
            replace(
                self._review_payload,
                displayed_config_id=(
                    self._state.displayed_config_id or self._review_payload.displayed_config_id
                ),
                trigger_source=self._state.trigger_source,
                no_result=True,
                error_message=str(error),
                judgment_payload=None,
            )
        )

    def handle_runtime_reanalysis_completed(self, payload: FactoryReviewPayload) -> None:
        if not self._owns_reanalysis_payload(payload):
            return
        if payload.no_result or payload.error_message:
            self.finish_reanalysis_failure(payload.error_message or "manual reanalysis failed")
            return
        self.finish_reanalysis_success(payload)

    def stop_active_run(self) -> bool:
        config_id = self._state.active_config_id
        if config_id is None or self._state.run_phase not in self._ACTIVE_RUN_PHASES:
            return False

        stopped = True
        test_manager = getattr(self.services, "test", None)
        stop_active_run = getattr(test_manager, "stop_active_run", None)
        if callable(stop_active_run):
            stopped = bool(stop_active_run(config_id=config_id))
        elif test_manager is not None:
            stop = getattr(test_manager, "stop", None)
            if callable(stop):
                stop(config_id=config_id)
            else:
                stopped = False

        if stopped:
            self._reset_review_context("active_run_stopped")
        return stopped

    def clear_for_next_run(self, reason: str = "next_run_accepted") -> None:
        self._reset_review_context(reason)

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
        self.bus.factory_runtime_aborted.connect(self.handle_runtime_aborted)
        self.bus.factory_runtime_reanalysis_completed.connect(
            self.handle_runtime_reanalysis_completed
        )

    def _accept_start(
        self,
        config_id: int,
        trigger_source: str,
        workorder: dict | None = None,
    ) -> bool:
        if not self.can_arm():
            return False

        payload = dict(workorder or {})
        payload["config_id"] = config_id
        payload["trigger_source"] = trigger_source
        start_payload = FactoryRuntimeStartPayload(
            config_id=config_id,
            trigger_source=trigger_source,
        )
        self._pending_start = start_payload

        def _on_start_accepted() -> None:
            self.bus.factory_runtime_start_accepted.emit(start_payload)

        start_accepted = True
        test_manager = getattr(self.services, "test", None)
        if test_manager is not None:
            start = getattr(test_manager, "start", None)
            if callable(start):
                start_accepted = bool(
                    start(payload, on_start_accepted=_on_start_accepted)
                )
            else:
                start_accepted = False
        else:
            _on_start_accepted()

        if not start_accepted:
            self._pending_start = None
            return False

        return True

    def _apply_review_payload(self, payload: FactoryReviewPayload) -> None:
        trigger_source = payload.trigger_source
        if trigger_source is None:
            trigger_source = self._state.trigger_source

        self._review_payload = replace(payload, trigger_source=trigger_source)
        self._state = replace(
            self._state,
            active_config_id=None,
            displayed_config_id=self._review_payload.displayed_config_id,
            run_phase="reviewing",
            trigger_source=trigger_source,
        )
        self.bus.factory_review_changed.emit(self._review_payload)
        self._emit_state()

    def _load_selected_configs(self, selected_config_ids: tuple[int, ...]) -> dict[int, dict]:
        config_service = getattr(self.services, "config_service", None)
        if config_service is None:
            return {
                config_id: {"config_id": config_id}
                for config_id in selected_config_ids
            }

        configs: dict[int, dict] = {}
        for config_id in selected_config_ids:
            try:
                configs[config_id] = config_service.load_config(config_id)
            except Exception:
                return {}
        return configs

    def _selected_configs_have_runtime_errors(self) -> bool:
        config_service = getattr(self.services, "config_service", None)
        validate = getattr(config_service, "validate_factory_runtime", None)
        if not callable(validate):
            return False
        return any(validate(config) for config in self._selected_configs.values())

    def _resolve_plc_config_id(self, plc_device_key: str) -> int | None:
        if not self.can_arm():
            return None

        matches = [
            config_id
            for config_id, config in self._selected_configs.items()
            if config.get("trigger_type") == "plc"
            and config.get("plc_device_key")
            and config.get("plc_device_key") == plc_device_key
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 2:
            return self._state.manual_primary_config_id
        return None

    def _resolve_scan_config_id(self) -> int | None:
        if not self.can_arm():
            return None

        matches = [
            config_id
            for config_id, config in self._selected_configs.items()
            if bool(config.get("scan_triggers_test"))
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 2:
            return self._state.manual_primary_config_id
        return None

    def _has_review_context(self) -> bool:
        return (
            self._review_payload != FactoryReviewPayload()
            or self._state.displayed_config_id is not None
            or self._state.run_phase in {"reviewing", "reanalyzing"}
        )

    def _reset_review_context(
        self,
        reason: str,
        *,
        state: FactoryControllerState | None = None,
    ) -> None:
        self._pending_start = None
        self._owned_runtime_config_id = None
        self._owned_reanalysis_config_id = None
        self._review_payload = FactoryReviewPayload()
        self._state = state or replace(
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

    def _matches_pending_start(self, payload: FactoryRuntimeStartPayload) -> bool:
        return (
            self._pending_start is not None
            and payload.config_id == self._pending_start.config_id
            and payload.trigger_source == self._pending_start.trigger_source
        )

    def _owns_active_runtime_config(self, config_id: int) -> bool:
        return (
            self._owned_runtime_config_id == config_id
            and self._state.active_config_id == config_id
            and self._state.run_phase in self._ACTIVE_RUN_PHASES
        )

    def _owns_active_review_payload(self, payload: FactoryReviewPayload) -> bool:
        config_id = payload.displayed_config_id
        return config_id is not None and self._owns_active_runtime_config(config_id)

    def _owns_reanalysis_payload(self, payload: FactoryReviewPayload) -> bool:
        config_id = payload.displayed_config_id
        return (
            config_id is not None
            and config_id == self._owned_reanalysis_config_id
            and self._state.displayed_config_id == config_id
            and self._state.run_phase == "reanalyzing"
        )
