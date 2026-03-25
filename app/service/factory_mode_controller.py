from __future__ import annotations

import uuid
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

_UNSET = object()


class FactoryModeController(QObject):
    """Owns the factory-mode page state and the UI-facing review contract."""

    __test__ = False
    _BLOCKED_START_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _ACTIVE_RUN_PHASES = frozenset({"running", "analyzing"})
    _BLOCKED_START_PHASE_REASONS = {
        "running": "当前测试运行中",
        "analyzing": "当前测试分析中",
        "reanalyzing": "当前正在重新分析",
    }

    def __init__(self, bus, services=None):
        super().__init__()
        self.bus = bus
        self.services = services
        self._state = FactoryControllerState()
        self._review_payload = FactoryReviewPayload()
        self._pending_start: tuple[int, str] | None = None
        self._pending_start_request_id: str | None = None
        self._dispatching_runtime_start_accepted_payload: FactoryRuntimeStartPayload | None = None
        self._owned_runtime_config_id: int | None = None
        self._owned_runtime_run_id: str | None = None
        self._owned_reanalysis_config_id: int | None = None
        self._owned_reanalysis_run_id: str | None = None
        self._last_plc_ingress: FactoryPlcIngressPayload | None = None
        self._last_scan_ingress: FactoryScanIngressPayload | None = None
        self._selected_configs: dict[int, dict] = {}
        self._selection_refresh_deferred = False
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

        if selection_changed and self._pending_start is not None:
            if not self._cancel_pending_start(emit_state=False):
                return self._state

        is_ready = self._reload_selected_configs(
            selected_config_ids=selected,
            manual_primary_config_id=manual_primary_config_id,
        )

        next_state = self._state_with_readiness(
            FactoryControllerState(
                selected_config_ids=selected,
                manual_primary_config_id=manual_primary_config_id,
                active_config_id=None,
                displayed_config_id=None,
                run_phase="armed" if is_ready else "not_ready",
                trigger_source=None,
            )
        )

        if has_review_context:
            self._reset_review_context("selection_changed", state=next_state)
        else:
            self._state = next_state
            self._emit_state()
        return self._state

    def refresh_selection(self) -> FactoryControllerState:
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            self._selection_refresh_deferred = True
            return self._state

        return self._refresh_selection_now()

    def _refresh_selection_now(self, *, emit_state: bool = True) -> FactoryControllerState:
        self._selection_refresh_deferred = False

        selected = tuple(self._state.selected_config_ids)
        manual_primary_config_id = self._state.manual_primary_config_id
        if not selected and manual_primary_config_id is None:
            return self._state

        is_ready = self._reload_selected_configs(
            selected_config_ids=selected,
            manual_primary_config_id=manual_primary_config_id,
        )
        if self._pending_start is not None and not is_ready:
            self._stale_pending_start()
        if self._has_review_context():
            self._state = self._state_with_readiness(self._state)
            if emit_state:
                self._emit_state()
            return self._state

        self._state = self._state_with_readiness(
            FactoryControllerState(
                selected_config_ids=selected,
                manual_primary_config_id=manual_primary_config_id,
                active_config_id=None,
                displayed_config_id=None,
                run_phase="armed" if is_ready else "not_ready",
                trigger_source=None,
            )
        )
        if emit_state:
            self._emit_state()
        return self._state

    def _apply_deferred_selection_refresh(self, *, emit_state: bool = False) -> None:
        if not self._selection_refresh_deferred:
            return
        self._refresh_selection_now(emit_state=emit_state)

    def can_arm(self) -> bool:
        if self._state.run_phase not in self._BLOCKED_START_PHASES:
            self._apply_deferred_selection_refresh()
        self._state = self._state_with_readiness(self._state)
        return self._state.start_ready

    def start_manual(self, workorder: dict | None = None) -> bool:
        config_id = self._state.manual_primary_config_id
        if config_id is None:
            return False
        return self._accept_start(config_id, "manual", workorder)

    def clear_display_state(self, reason: str) -> None:
        self.clear_for_next_run(reason)

    def handle_plc_event(self, payload: FactoryPlcIngressPayload) -> bool:
        self._last_plc_ingress = payload
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            self._log_ingress_ignored(
                event_type="PLC",
                reason=self._state.run_phase,
                plc_device_key=payload.plc_device_key,
            )
            return False
        if not self.can_arm():
            self._log_ingress_ignored(
                event_type="PLC",
                reason="not armed",
                plc_device_key=payload.plc_device_key,
            )
            return False
        config_id = self._resolve_plc_config_id(payload.plc_device_key)
        if config_id is None:
            self._log_ingress_ignored(
                event_type="PLC",
                reason="no match",
                plc_device_key=payload.plc_device_key,
            )
            return False
        return self._accept_start(config_id, "plc")

    def handle_scan_event(self, payload: FactoryScanIngressPayload) -> bool:
        self._last_scan_ingress = payload
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            self._log_ingress_ignored(
                event_type="scan",
                reason=self._state.run_phase,
                scan_text=payload.scan_text,
            )
            return False
        if not self.can_arm():
            self._log_ingress_ignored(
                event_type="scan",
                reason="not armed",
                scan_text=payload.scan_text,
            )
            return False
        config_id = self._resolve_scan_config_id()
        if config_id is None:
            self._log_ingress_ignored(
                event_type="scan",
                reason="no match",
                scan_text=payload.scan_text,
            )
            return False
        return self._accept_start(config_id, "scan")

    def handle_runtime_started(self, payload: FactoryRuntimeStartPayload) -> None:
        if not self._matches_pending_start(payload):
            return

        self._pending_start = None
        self._pending_start_request_id = None
        if self._has_review_context():
            self._reset_review_context("next_run_accepted")

        self._owned_runtime_config_id = payload.config_id
        self._owned_runtime_run_id = payload.run_id
        self._state = self._state_with_readiness(
            replace(
                self._state,
                active_config_id=payload.config_id,
                displayed_config_id=payload.config_id,
                run_phase="running",
                trigger_source=payload.trigger_source,
            )
        )
        self._emit_state()

    def handle_runtime_start_accepted(self, payload: FactoryRuntimeStartPayload) -> None:
        if payload is not self._dispatching_runtime_start_accepted_payload:
            return
        self.handle_runtime_started(payload)

    def handle_runtime_analyzing(self, payload: FactoryRuntimeConfigPayload) -> None:
        if not self._owns_runtime_payload(
            config_id=payload.config_id,
            run_id=payload.run_id,
            allowed_phases=self._ACTIVE_RUN_PHASES,
        ):
            return

        self._state = self._state_with_readiness(
            replace(
                self._state,
                active_config_id=payload.config_id,
                displayed_config_id=payload.config_id,
                run_phase="analyzing",
            )
        )
        self._emit_state()

    def handle_runtime_analyzing_entered(self, payload: FactoryRuntimeConfigPayload) -> None:
        self.handle_runtime_analyzing(payload)

    def handle_runtime_review_ready(self, payload: FactoryReviewPayload) -> None:
        if not self._owns_active_review_payload(payload):
            return

        self._owned_runtime_config_id = None
        self._owned_runtime_run_id = None
        self._apply_review_payload(payload)

    def handle_runtime_auto_analysis_succeeded(self, payload: FactoryReviewPayload) -> None:
        self.handle_runtime_review_ready(payload)

    def handle_runtime_no_result(self, payload: FactoryReviewPayload) -> None:
        if not self._owns_active_review_payload(payload):
            return

        self._owned_runtime_config_id = None
        self._owned_runtime_run_id = None
        self._apply_review_payload(payload)

    def handle_runtime_auto_analysis_no_result(self, payload: FactoryReviewPayload) -> None:
        self.handle_runtime_no_result(payload)

    def handle_runtime_auto_analysis_error(self, payload: FactoryReviewPayload) -> None:
        self.handle_runtime_no_result(payload)

    def handle_runtime_aborted(self, payload) -> None:
        if not self._owns_runtime_payload(
            config_id=payload.config_id,
            run_id=payload.run_id,
            allowed_phases=self._ACTIVE_RUN_PHASES,
        ):
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
        self._owned_reanalysis_run_id = self._review_payload.run_id
        self._review_payload = replace(
            self._review_payload,
            displayed_config_id=config_id,
            trigger_source=self._state.trigger_source,
            no_result=False,
            error_message=None,
            judgment_payload=None,
        )
        self._state = self._state_with_readiness(
            replace(
                self._state,
                active_config_id=None,
                displayed_config_id=config_id,
                run_phase="reanalyzing",
            )
        )
        self.bus.factory_review_changed.emit(self._review_payload)
        self._emit_state()
        test_manager = getattr(self.services, "test", None)
        if test_manager is not None:
            try:
                test_manager.rejudge(config_id, start_sec, end_sec)
            except Exception as exc:
                self.finish_reanalysis_failure(str(exc))
                return False
        return True

    def finish_reanalysis_success(self, payload: FactoryReviewPayload) -> None:
        self._owned_reanalysis_config_id = None
        self._owned_reanalysis_run_id = None
        self._apply_review_payload(payload)

    def finish_reanalysis_failure(self, error: Exception | str) -> None:
        self._owned_reanalysis_config_id = None
        self._owned_reanalysis_run_id = None
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
        if self._pending_start is not None:
            return self._cancel_pending_start()

        config_id = self._state.active_config_id
        if config_id is None or self._state.run_phase not in self._ACTIVE_RUN_PHASES:
            return False

        stopped = True
        test_manager = getattr(self.services, "test", None)
        stop_active_run = getattr(test_manager, "stop_active_run", None)
        try:
            if callable(stop_active_run):
                stopped = bool(stop_active_run(config_id=config_id))
            elif test_manager is not None:
                stop = getattr(test_manager, "stop", None)
                if callable(stop):
                    stop(config_id=config_id)
                else:
                    stopped = False
        except Exception:
            stopped = False

        if stopped:
            self._reset_review_context("active_run_stopped")
        return stopped

    def clear_for_next_run(self, reason: str = "next_run_accepted") -> None:
        if self._pending_start is not None or self._state.run_phase in self._BLOCKED_START_PHASES:
            return
        self._reset_review_context(reason)

    def _connect_bus(self) -> None:
        if hasattr(self.bus, "config_changed"):
            self.bus.config_changed.connect(self.refresh_selection)
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
        if not self.can_arm() or self._backend_session_running():
            return False

        payload = dict(workorder or {})
        payload["config_id"] = config_id
        payload["trigger_source"] = trigger_source
        request_id = f"factory-start-{uuid.uuid4().hex}"
        payload["factory_start_request_id"] = request_id
        self._pending_start = (config_id, trigger_source)
        self._pending_start_request_id = request_id

        def _on_start_accepted(run_id: str) -> None:
            if not self._matches_pending_start_request(
                config_id=config_id,
                trigger_source=trigger_source,
                request_id=request_id,
            ):
                return
            accepted_payload = FactoryRuntimeStartPayload(
                config_id=config_id,
                run_id=run_id,
                trigger_source=trigger_source,
            )
            self._dispatching_runtime_start_accepted_payload = accepted_payload
            try:
                self.bus.factory_runtime_start_accepted.emit(accepted_payload)
            finally:
                if self._dispatching_runtime_start_accepted_payload is accepted_payload:
                    self._dispatching_runtime_start_accepted_payload = None

        start_accepted = True
        test_manager = getattr(self.services, "test", None)
        if test_manager is not None:
            start = getattr(test_manager, "start", None)
            if callable(start):
                try:
                    start_accepted = bool(
                        start(payload, on_start_accepted=_on_start_accepted)
                    )
                except Exception:
                    self._pending_start = None
                    self._pending_start_request_id = None
                    return False
            else:
                start_accepted = False
        else:
            _on_start_accepted(self._make_fallback_run_id(config_id))

        if not start_accepted:
            self._pending_start = None
            self._pending_start_request_id = None
            return False

        if self._pending_start is not None:
            self._state = self._state_with_readiness(self._state)
            self._emit_state()
        return True

    def _apply_review_payload(self, payload: FactoryReviewPayload) -> None:
        self._apply_deferred_selection_refresh()
        trigger_source = payload.trigger_source
        if trigger_source is None:
            trigger_source = self._state.trigger_source

        self._review_payload = replace(payload, trigger_source=trigger_source)
        self._state = self._state_with_readiness(
            replace(
                self._state,
                active_config_id=None,
                displayed_config_id=self._review_payload.displayed_config_id,
                run_phase="reviewing",
                trigger_source=trigger_source,
            )
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

    def _reload_selected_configs(
        self,
        *,
        selected_config_ids: tuple[int, ...],
        manual_primary_config_id: int | None,
    ) -> bool:
        self._selected_configs = {}
        if (
            len(selected_config_ids) == 2
            and len(set(selected_config_ids)) == 2
            and manual_primary_config_id in selected_config_ids
        ):
            self._selected_configs = self._load_selected_configs(selected_config_ids)
        return self._selection_is_start_ready(
            selected_config_ids=selected_config_ids,
            manual_primary_config_id=manual_primary_config_id,
        )

    def _selected_configs_have_runtime_errors(self) -> bool:
        return bool(self._selected_config_runtime_errors())

    def _selected_config_runtime_errors(self) -> list[str]:
        config_service = getattr(self.services, "config_service", None)
        validate = getattr(config_service, "validate_factory_runtime", None)
        if not callable(validate):
            return []
        errors: list[str] = []
        for config in self._selected_configs.values():
            errors.extend(validate(config))
        return errors

    def _selection_is_start_ready(
        self,
        *,
        selected_config_ids: tuple[int, ...] | object = _UNSET,
        manual_primary_config_id: int | None | object = _UNSET,
    ) -> bool:
        selected = (
            tuple(selected_config_ids)
            if selected_config_ids is not _UNSET
            else tuple(self._state.selected_config_ids)
        )
        primary_config_id = (
            manual_primary_config_id
            if manual_primary_config_id is not _UNSET
            else self._state.manual_primary_config_id
        )
        return (
            len(selected) == 2
            and len(set(selected)) == 2
            and primary_config_id in selected
            and len(self._selected_configs) == len(selected)
            and not self._selected_config_runtime_errors()
        )

    def _selection_readiness(
        self,
        *,
        selected_config_ids: tuple[int, ...] | object = _UNSET,
        manual_primary_config_id: int | None | object = _UNSET,
    ) -> tuple[bool, str | None]:
        selected = (
            tuple(selected_config_ids)
            if selected_config_ids is not _UNSET
            else tuple(self._state.selected_config_ids)
        )
        primary_config_id = (
            manual_primary_config_id
            if manual_primary_config_id is not _UNSET
            else self._state.manual_primary_config_id
        )
        if len(selected) != 2 or len(set(selected)) != 2:
            return False, "请选择两个不同配置"
        if primary_config_id not in selected:
            return False, "请先选择手动主配置"
        if len(self._selected_configs) != len(selected):
            return False, "工厂模式未就绪"
        runtime_errors = self._selected_config_runtime_errors()
        if runtime_errors:
            return False, runtime_errors[0]
        return True, None

    def _start_readiness(
        self,
        *,
        state: FactoryControllerState,
        selection_ready: bool,
        selection_ready_reason: str | None,
    ) -> tuple[bool, str | None]:
        blocked_phase_reason = self._BLOCKED_START_PHASE_REASONS.get(state.run_phase)
        if blocked_phase_reason is not None:
            return False, blocked_phase_reason
        if not selection_ready:
            return False, selection_ready_reason
        if self._pending_start is not None:
            return False, "启动请求处理中"
        if self._backend_session_running():
            return False, "当前测试运行中"
        if state.run_phase not in {"armed", "reviewing"}:
            return False, "工厂模式未就绪"
        return True, None

    def _state_with_readiness(self, state: FactoryControllerState) -> FactoryControllerState:
        selection_ready, selection_ready_reason = self._selection_readiness(
            selected_config_ids=state.selected_config_ids,
            manual_primary_config_id=state.manual_primary_config_id,
        )
        start_ready, start_ready_reason = self._start_readiness(
            state=state,
            selection_ready=selection_ready,
            selection_ready_reason=selection_ready_reason,
        )
        return replace(
            state,
            selection_ready=selection_ready,
            selection_ready_reason=selection_ready_reason,
            start_ready=start_ready,
            start_ready_reason=start_ready_reason,
        )

    def _resolve_plc_config_id(self, plc_device_key: str) -> int | None:
        normalized_plc_key = self._normalize_optional_text(plc_device_key)
        if normalized_plc_key is None:
            return None
        matches = [
            config_id
            for config_id, config in self._selected_configs.items()
            if self._normalize_optional_text(config.get("plc_device_key"))
            == normalized_plc_key
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 2:
            return self._state.manual_primary_config_id
        return None

    def _resolve_scan_config_id(self) -> int | None:
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

    @staticmethod
    def _normalize_optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    def _reset_review_context(
        self,
        reason: str,
        *,
        state: FactoryControllerState | None = None,
    ) -> None:
        self._pending_start = None
        self._pending_start_request_id = None
        self._owned_runtime_config_id = None
        self._owned_runtime_run_id = None
        self._owned_reanalysis_config_id = None
        self._owned_reanalysis_run_id = None
        self._apply_deferred_selection_refresh()
        self._review_payload = FactoryReviewPayload()
        self._state = self._state_with_readiness(
            state
            or replace(
                self._state,
                active_config_id=None,
                displayed_config_id=None,
                run_phase="armed" if self._selection_is_start_ready() else "not_ready",
                trigger_source=None,
            )
        )
        self.bus.factory_display_reset.emit(reason)
        self.bus.factory_review_changed.emit(self._review_payload)
        self._emit_state()

    def _emit_state(self) -> None:
        self.bus.factory_state_changed.emit(self._state)

    def _matches_pending_start(self, payload: FactoryRuntimeStartPayload) -> bool:
        return (
            self._pending_start is not None
            and self._pending_start_request_id is not None
            and payload.config_id == self._pending_start[0]
            and payload.trigger_source == self._pending_start[1]
        )

    def _matches_pending_start_request(
        self,
        *,
        config_id: int,
        trigger_source: str,
        request_id: str,
    ) -> bool:
        return (
            self._pending_start is not None
            and self._pending_start_request_id is not None
            and config_id == self._pending_start[0]
            and trigger_source == self._pending_start[1]
            and self._pending_start_request_id == request_id
        )

    def _cancel_pending_start(self, *, emit_state: bool = True) -> bool:
        request_id = self._pending_start_request_id
        if self._pending_start is None:
            self._pending_start_request_id = None
            return True
        if not request_id:
            return False

        test_manager = getattr(self.services, "test", None)
        cancel_pending_start = getattr(test_manager, "cancel_pending_start", None)
        if not callable(cancel_pending_start):
            return False

        try:
            canceled = bool(
                cancel_pending_start(
                    request_id=request_id,
                    config_id=self._pending_start[0],
                )
            )
        except Exception:
            return False

        if not canceled:
            return False

        self._pending_start = None
        self._pending_start_request_id = None
        if emit_state:
            self._state = self._state_with_readiness(self._state)
            self._emit_state()
        return True

    def _stale_pending_start(self) -> None:
        if self._pending_start is None:
            self._pending_start_request_id = None
            return
        if self._cancel_pending_start(emit_state=False):
            return
        self._pending_start = None
        self._pending_start_request_id = None

    def _backend_session_running(self) -> bool:
        test_manager = getattr(self.services, "test", None)
        is_running = getattr(test_manager, "is_running", None)
        if not callable(is_running):
            return False

        try:
            return bool(is_running())
        except Exception:
            return False

    def _log_ingress_ignored(self, *, event_type: str, reason: str, **details) -> None:
        log_signal = getattr(self.bus, "log", None)
        emit = getattr(log_signal, "emit", None)
        if not callable(emit):
            return

        detail_parts = [
            f"{key}={value}"
            for key, value in details.items()
            if value is not None and value != ""
        ]
        detail_suffix = ""
        if detail_parts:
            detail_suffix = f" ({', '.join(detail_parts)})"
        emit(f"[FactoryModeController] {event_type} event ignored: {reason}{detail_suffix}")

    def _owns_runtime_payload(
        self,
        *,
        config_id: int,
        run_id: str | None,
        allowed_phases: frozenset[str],
    ) -> bool:
        return (
            run_id is not None
            and
            self._owned_runtime_config_id == config_id
            and self._owned_runtime_run_id == run_id
            and self._state.active_config_id == config_id
            and self._state.run_phase in allowed_phases
        )

    def _owns_active_review_payload(self, payload: FactoryReviewPayload) -> bool:
        config_id = payload.displayed_config_id
        return (
            config_id is not None
            and self._owns_runtime_payload(
                config_id=config_id,
                run_id=payload.run_id,
                allowed_phases=frozenset({"analyzing"}),
            )
        )

    def _owns_reanalysis_payload(self, payload: FactoryReviewPayload) -> bool:
        config_id = payload.displayed_config_id
        if (
            config_id is None
            or config_id != self._owned_reanalysis_config_id
            or self._state.displayed_config_id != config_id
            or self._state.run_phase != "reanalyzing"
        ):
            return False

        if payload.run_id is None:
            return (
                bool(payload.no_result or payload.error_message)
                and self._owned_reanalysis_run_id is not None
                and self._review_payload.run_id == self._owned_reanalysis_run_id
            )

        return (
            payload.run_id == self._owned_reanalysis_run_id
            and self._review_payload.run_id == payload.run_id
        )

    @staticmethod
    def _make_fallback_run_id(config_id: int) -> str:
        return f"factory-run-{config_id}-{uuid.uuid4().hex}"
