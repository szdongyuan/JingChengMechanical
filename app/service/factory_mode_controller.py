from __future__ import annotations

import uuid
from dataclasses import replace

from PyQt6.QtCore import QObject

from app.service.analysis.review_payloads import build_summary_payload
from app.services_container import (
    FactoryControllerState,
    FactoryPlcIngressPayload,
    FactoryReviewPayload,
    FactoryRuntimeConfigPayload,
    FactoryRuntimeStartPayload,
    FactoryRuntimeStoppedPayload,
    FactoryScanIngressPayload,
    FactoryStartBlockedPayload,
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
        self._invalid_selection_warning_active = False
        self._controller_selection_notice_active = False
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
    ) -> FactoryControllerState:
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            return self._state

        selected = tuple(selected_config_ids)
        selection_changed = selected != self._state.selected_config_ids
        has_review_context = self._has_review_context()
        if has_review_context and not selection_changed:
            return self._state

        if selection_changed and self._pending_start is not None:
            if not self._cancel_pending_start(emit_state=False):
                return self._state

        if selection_changed:
            self._invalid_selection_warning_active = False

        is_ready = self._reload_selected_configs(selected_config_ids=selected)

        next_state = self._state_with_readiness(
            FactoryControllerState(
                selected_config_ids=selected,
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

    def restore_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        *,
        had_missing_configs: bool,
    ) -> FactoryControllerState:
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            return self._state

        self._invalid_selection_warning_active = bool(had_missing_configs)
        selected = tuple(selected_config_ids)
        is_ready = self._reload_selected_configs(selected_config_ids=selected)
        self._state = self._state_with_readiness(
            FactoryControllerState(
                selected_config_ids=selected,
                active_config_id=None,
                displayed_config_id=None,
                run_phase="armed" if is_ready else "not_ready",
                trigger_source=None,
            )
        )
        self._emit_state()
        return self._state

    def refresh_selection(self) -> FactoryControllerState:
        selected = tuple(self._state.selected_config_ids)
        pruned_selected, had_missing_configs = self._prune_missing_selected_config_ids(selected)
        if had_missing_configs:
            self._invalid_selection_warning_active = True
            self._persist_runtime_selected_config_ids(pruned_selected)
        return self._refresh_selection_now(
            selected_config_ids=pruned_selected,
        )

    def _refresh_selection_now(
        self,
        *,
        selected_config_ids: tuple[int, ...] | object = _UNSET,
        emit_state: bool = True,
    ) -> FactoryControllerState:
        self._selection_refresh_deferred = False

        selected = (
            tuple(selected_config_ids)
            if selected_config_ids is not _UNSET
            else tuple(self._state.selected_config_ids)
        )

        is_ready = self._reload_selected_configs(selected_config_ids=selected)
        preserve_current_context = (
            self._has_review_context()
            or self._pending_start is not None
            or self._backend_session_running()
        )
        if preserve_current_context:
            self._state = self._state_with_readiness(
                replace(
                    self._state,
                    selected_config_ids=selected,
                )
            )
            if emit_state:
                self._emit_state()
            return self._state

        self._state = self._state_with_readiness(
            FactoryControllerState(
                selected_config_ids=selected,
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
        previous_state = self._state
        if self._state.run_phase not in self._BLOCKED_START_PHASES and not self._start_feedback_is_busy():
            self._apply_deferred_selection_refresh()
        next_state = self._state_with_readiness(self._state)
        if next_state != self._state:
            self._state = next_state
        if self._state != previous_state:
            self._emit_state()
        return self._state.start_ready

    def start_manual(self, workorder: dict | None = None) -> bool:
        if self._start_feedback_is_busy():
            return False
        if not self.can_arm():
            return self._emit_blocked_start("manual")
        selected = tuple(self._state.selected_config_ids)
        if not selected:
            return self._emit_blocked_start("manual")
        config_id = selected[0]
        return self._accept_start(config_id, "manual", workorder)

    def clear_display_state(self, reason: str) -> None:
        self.clear_for_next_run(reason)

    def handle_plc_event(self, payload: FactoryPlcIngressPayload) -> bool:
        self._last_plc_ingress = payload
        if self._start_feedback_is_busy():
            self._log_ingress_ignored(
                event_type="PLC",
                reason=self._busy_state_reason_label(),
                plc_device_key=payload.plc_device_key,
            )
            return False
        if not self.can_arm():
            self._log_ingress_ignored(
                event_type="PLC",
                reason="not armed",
                plc_device_key=payload.plc_device_key,
            )
            return self._emit_blocked_start("plc")
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
        if self._start_feedback_is_busy():
            self._log_ingress_ignored(
                event_type="scan",
                reason=self._busy_state_reason_label(),
                scan_text=payload.scan_text,
            )
            return False
        if not self.can_arm():
            self._log_ingress_ignored(
                event_type="scan",
                reason="not armed",
                scan_text=payload.scan_text,
            )
            return self._emit_blocked_start("scan")
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

    def handle_runtime_stopped(self, payload: FactoryRuntimeStoppedPayload) -> None:
        if not self._owns_runtime_payload(
            config_id=payload.config_id,
            run_id=payload.run_id,
            allowed_phases=self._ACTIVE_RUN_PHASES,
        ):
            return

        self._owned_runtime_config_id = None
        self._owned_runtime_run_id = None
        self._state = self._state_with_readiness(
            replace(
                self._state,
                active_config_id=None,
                displayed_config_id=payload.config_id,
                run_phase="stopped",
                trigger_source=payload.trigger_source,
            )
        )
        self._emit_state()

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
            summary_payload=None,
            chart_payload=None,
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

    def finish_reanalysis_failure(
        self,
        error: Exception | str,
        *,
        summary_payload: dict | None = None,
    ) -> None:
        self._owned_reanalysis_config_id = None
        self._owned_reanalysis_run_id = None
        error_message = str(error)
        summary_payload = self._normalize_no_result_summary(
            error_message=error_message,
            summary_payload=summary_payload,
        )
        self._apply_review_payload(
            replace(
                self._review_payload,
                displayed_config_id=(
                    self._state.displayed_config_id or self._review_payload.displayed_config_id
                ),
                trigger_source=self._state.trigger_source,
                no_result=True,
                error_message=self._summary_error_message(
                    error_message=error_message,
                    summary_payload=summary_payload,
                ),
                judgment_payload=None,
                summary_payload=summary_payload,
                chart_payload=None,
            )
        )

    def handle_runtime_reanalysis_completed(self, payload: FactoryReviewPayload) -> None:
        if not self._owns_reanalysis_payload(payload):
            return
        if isinstance(payload.summary_payload, dict):
            self.finish_reanalysis_success(self._merge_reanalysis_review_payload(payload))
            return
        if payload.no_result or payload.error_message:
            self.finish_reanalysis_failure(
                payload.error_message or "manual reanalysis failed",
                summary_payload=payload.summary_payload,
            )
            return
        self.finish_reanalysis_success(self._merge_reanalysis_review_payload(payload))

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
        if hasattr(self.bus, "factory_runtime_auto_analysis_cleanup_settled"):
            self.bus.factory_runtime_auto_analysis_cleanup_settled.connect(
                self.handle_runtime_auto_analysis_cleanup_settled
            )
        self.bus.factory_runtime_stopped.connect(self.handle_runtime_stopped)
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
            ),
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
    ) -> bool:
        self._selected_configs = {}
        if selected_config_ids:
            self._selected_configs = self._load_selected_configs(selected_config_ids)
        return self._selection_is_start_ready(selected_config_ids=selected_config_ids)

    def _runtime_errors_for(self, config_id: int) -> list[str]:
        config_service = getattr(self.services, "config_service", None)
        validate = getattr(config_service, "validate_factory_runtime", None)
        config = self._selected_configs.get(config_id)
        if config is None or not callable(validate):
            return []
        return list(validate(config))

    def _first_selected_config_runtime_error(
        self,
        *,
        selected_config_ids: tuple[int, ...] | object = _UNSET,
    ) -> str | None:
        selected = (
            tuple(selected_config_ids)
            if selected_config_ids is not _UNSET
            else tuple(self._state.selected_config_ids)
        )
        for config_id in selected:
            errors = self._runtime_errors_for(config_id)
            if errors:
                return errors[0]
        return None

    def _selection_is_start_ready(
        self,
        *,
        selected_config_ids: tuple[int, ...] | object = _UNSET,
    ) -> bool:
        selected = (
            tuple(selected_config_ids)
            if selected_config_ids is not _UNSET
            else tuple(self._state.selected_config_ids)
        )
        ready, _ = self._selection_readiness(selected_config_ids=selected)
        return ready

    def _selection_readiness(
        self,
        *,
        selected_config_ids: tuple[int, ...] | object = _UNSET,
    ) -> tuple[bool, str | None]:
        selected = (
            tuple(selected_config_ids)
            if selected_config_ids is not _UNSET
            else tuple(self._state.selected_config_ids)
        )
        if self._invalid_selection_warning_active:
            return False, "配置已失效"
        if not selected:
            return False, "请至少启用一个配置"
        if len(self._selected_configs) != len(selected):
            return False, "配置加载失败"
        runtime_error = self._first_selected_config_runtime_error(selected_config_ids=selected)
        if runtime_error is not None:
            return False, runtime_error
        return True, None

    def _start_readiness(
        self,
        *,
        state: FactoryControllerState,
        selection_ready: bool,
        selection_ready_reason: str | None,
        ignore_backend_session_running: bool = False,
    ) -> tuple[bool, str | None]:
        blocked_phase_reason = self._BLOCKED_START_PHASE_REASONS.get(state.run_phase)
        if blocked_phase_reason is not None:
            return False, blocked_phase_reason
        if self._pending_start is not None:
            return False, "启动请求处理中"
        if not ignore_backend_session_running and self._backend_session_running():
            return False, "当前测试运行中"
        if not selection_ready:
            return False, selection_ready_reason
        if state.run_phase not in {"armed", "reviewing", "stopped"}:
            return False, "工厂模式未就绪"
        return True, None

    def _state_with_readiness(
        self,
        state: FactoryControllerState,
        *,
        ignore_backend_session_running: bool = False,
    ) -> FactoryControllerState:
        selection_ready, selection_ready_reason = self._selection_readiness(
            selected_config_ids=state.selected_config_ids,
        )
        start_ready, start_ready_reason = self._start_readiness(
            state=state,
            selection_ready=selection_ready,
            selection_ready_reason=selection_ready_reason,
            ignore_backend_session_running=ignore_backend_session_running,
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
        if matches:
            return matches[0]
        return None

    def _resolve_scan_config_id(self) -> int | None:
        matches = [
            config_id
            for config_id, config in self._selected_configs.items()
            if bool(config.get("scan_triggers_test"))
        ]
        if matches:
            return matches[0]
        return None

    def _has_review_context(self) -> bool:
        return (
            self._review_payload != FactoryReviewPayload()
            or self._state.displayed_config_id is not None
            or self._state.run_phase in {"reviewing", "reanalyzing", "stopped"}
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

    def _emit_blocked_start(self, trigger_source: str) -> bool:
        reason = self._state.start_ready_reason
        if reason is None:
            return False
        self.bus.factory_start_blocked.emit(
            FactoryStartBlockedPayload(
                reason=reason,
                trigger_source=trigger_source,
            )
        )
        return False

    def _emit_state(self) -> None:
        self.bus.factory_state_changed.emit(self._state)

    def _start_feedback_is_busy(self) -> bool:
        return (
            self._state.run_phase in self._BLOCKED_START_PHASES
            or self._pending_start is not None
            or self._backend_session_running()
        )

    def _busy_state_reason_label(self) -> str:
        if self._pending_start is not None:
            return "pending start"
        if self._state.run_phase in self._BLOCKED_START_PHASES:
            return self._state.run_phase
        if self._backend_session_running():
            return "backend running"
        return self._state.run_phase

    def _prune_missing_selected_config_ids(
        self,
        selected_config_ids: tuple[int, ...],
    ) -> tuple[tuple[int, ...], bool]:
        config_service = getattr(self.services, "config_service", None)
        list_configs = getattr(config_service, "list_configs", None)
        if not callable(list_configs):
            return selected_config_ids, False

        try:
            config_rows = list_configs()
        except Exception:
            return selected_config_ids, False

        existing_config_ids = {
            row.get("config_id")
            for row in config_rows
            if isinstance(row, dict) and isinstance(row.get("config_id"), int)
        }
        pruned_selected_config_ids = tuple(
            config_id for config_id in selected_config_ids if config_id in existing_config_ids
        )
        return pruned_selected_config_ids, pruned_selected_config_ids != selected_config_ids

    def _persist_runtime_selected_config_ids(
        self,
        selected_config_ids: tuple[int, ...],
    ) -> None:
        selection_settings = getattr(self.services, "factory_selection_settings", None)
        save_selected_config_ids = getattr(selection_settings, "save_selected_config_ids", None)
        if not callable(save_selected_config_ids):
            return

        error_message = save_selected_config_ids(selected_config_ids)
        if error_message:
            self._controller_selection_notice_active = True
            self.bus.factory_selection_notice.emit(error_message)
            return
        if self._controller_selection_notice_active:
            self._controller_selection_notice_active = False
            self.bus.factory_selection_notice.emit("")

    def handle_runtime_auto_analysis_cleanup_settled(
        self,
        payload: FactoryRuntimeConfigPayload,
    ) -> None:
        if self._state.run_phase != "reviewing":
            return
        if self._state.displayed_config_id != payload.config_id:
            return
        if self._review_payload.run_id != payload.run_id:
            return

        next_state = self._state_with_readiness(self._state)
        if next_state == self._state:
            return
        self._state = next_state
        self._emit_state()

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
            self._apply_deferred_selection_refresh()
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
        if self._state.run_phase != "reanalyzing":
            return False

        if payload.displayed_config_id is None and payload.run_id is None:
            return False

        config_id = (
            payload.displayed_config_id
            if payload.displayed_config_id is not None
            else self._owned_reanalysis_config_id
        )
        if (
            config_id is None
            or config_id != self._owned_reanalysis_config_id
            or self._state.displayed_config_id != config_id
        ):
            return False

        run_id = payload.run_id if payload.run_id is not None else self._owned_reanalysis_run_id
        if run_id is None:
            return self._review_payload.run_id is None

        return (
            run_id == self._owned_reanalysis_run_id
            and self._review_payload.run_id == run_id
        )

    @staticmethod
    def _make_fallback_run_id(config_id: int) -> str:
        return f"factory-run-{config_id}-{uuid.uuid4().hex}"

    @staticmethod
    def _normalize_no_result_summary(
        *,
        error_message: str | None,
        summary_payload: dict | None,
    ) -> dict:
        if isinstance(summary_payload, dict):
            return summary_payload
        return build_summary_payload(
            config={},
            judgment_payload=None,
            error_message=error_message,
        )

    @classmethod
    def _summary_error_message(
        cls,
        *,
        error_message: str | None,
        summary_payload: dict | None,
    ) -> str | None:
        if isinstance(summary_payload, dict):
            summary_error_message = cls._normalize_optional_text(
                summary_payload.get("error_message")
            )
            if summary_error_message is not None:
                return summary_error_message
        normalized_error_message = cls._normalize_optional_text(error_message)
        if normalized_error_message is not None:
            return normalized_error_message
        return normalized_error_message

    def _merge_reanalysis_review_payload(
        self,
        payload: FactoryReviewPayload,
    ) -> FactoryReviewPayload:
        return replace(
            payload,
            displayed_config_id=(
                payload.displayed_config_id
                or self._state.displayed_config_id
                or self._review_payload.displayed_config_id
            ),
            run_id=payload.run_id or self._review_payload.run_id,
            trigger_source=(
                payload.trigger_source
                or self._state.trigger_source
                or self._review_payload.trigger_source
            ),
            captured_duration_sec=(
                payload.captured_duration_sec
                if payload.captured_duration_sec is not None
                else self._review_payload.captured_duration_sec
            ),
            default_window=payload.default_window or self._review_payload.default_window,
            fallback_window=payload.fallback_window or self._review_payload.fallback_window,
            manual_reanalysis_allowed=(
                payload.manual_reanalysis_allowed or self._review_payload.manual_reanalysis_allowed
            ),
            error_message=self._summary_error_message(
                error_message=payload.error_message,
                summary_payload=payload.summary_payload,
            ),
        )
