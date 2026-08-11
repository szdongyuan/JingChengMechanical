from __future__ import annotations

import time
from dataclasses import replace

from PyQt6.QtCore import QObject

from app.service.analysis.test_session import ManualLabelUpdateResult
from app.service.factory_selection_settings import (
    PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
    PRODUCTION_SELECTION_LEASE_NOTICE,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    StartStatus,
    TriggerSource,
)
from app.services_container import (
    FactoryControllerState,
    FactoryPlcIngressPayload,
    FactoryReviewPayload,
    FactoryScanIngressPayload,
    FactoryStartBlockedPayload,
)


_ACCEPTED_STARTS = frozenset(
    {
        StartStatus.ACCEPTED_ACTIVE,
        StartStatus.ACCEPTED_TERMINAL,
    }
)


class FactoryModeController(QObject):
    """Narrow UI readiness and ingress-intent adapter.

    Run admission, capacity, lifecycle, review state, and result operations
    belong to the final Coordinator and the Dashboard-owned history store.
    """

    __test__ = False

    def __init__(
        self,
        bus,
        services=None,
        *,
        monotonic_ns=time.monotonic_ns,
        prepared_selected_config_ids: tuple[int, ...] = (),
        prepared_config_routes: tuple[
            tuple[int, int | None], ...
        ] = (),
    ) -> None:
        super().__init__()
        if not callable(monotonic_ns):
            raise ValueError("monotonic_ns must be callable")
        self.bus = bus
        self.services = services
        self._monotonic_ns = monotonic_ns
        prepared_selection = tuple(prepared_selected_config_ids)
        if any(type(config_id) is not int for config_id in prepared_selection):
            raise ValueError("prepared selected config IDs must be integers")
        prepared_routes = self._normalize_routes(prepared_config_routes)
        if prepared_routes:
            if tuple(config_id for config_id, _plc_id in prepared_routes) != (
                prepared_selection
            ):
                raise ValueError(
                    "prepared config routes must match prepared selection"
                )
        else:
            prepared_routes = tuple(
                (config_id, None) for config_id in prepared_selection
            )
        self._prepared_config_routes = prepared_routes
        self._state = FactoryControllerState(
            selected_config_ids=prepared_selection,
            run_phase="armed" if prepared_selection else "not_ready",
            selection_ready=bool(prepared_selection),
            start_ready=bool(prepared_selection),
        )
        self._selected_configs: dict[int, dict] = {
            config_id: {"config_id": config_id}
            for config_id in prepared_selection
        }
        self._restored_selection_has_missing_configs = False
        self._selection_restart_required = False
        self._last_plc_ingress: FactoryPlcIngressPayload | None = None
        self._last_scan_ingress: FactoryScanIngressPayload | None = None
        self._connect_bus()

    @property
    def state(self) -> FactoryControllerState:
        return self._state

    @property
    def review_payload(self) -> FactoryReviewPayload:
        return FactoryReviewPayload()

    @property
    def last_plc_ingress(self) -> FactoryPlcIngressPayload | None:
        return self._last_plc_ingress

    @property
    def last_scan_ingress(self) -> FactoryScanIngressPayload | None:
        return self._last_scan_ingress

    @property
    def selection_restart_required(self) -> bool:
        return self._selection_restart_required

    def set_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
    ) -> FactoryControllerState:
        selected = tuple(selected_config_ids)
        if bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            return self._set_prepared_selection(selected)
        self._restored_selection_has_missing_configs = False
        self._selected_configs = self._load_selected_configs(selected)
        ready, reason = self._selection_readiness(selected)
        self._state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if ready else "not_ready",
            selection_ready=ready,
            selection_ready_reason=reason,
            start_ready=ready,
            start_ready_reason=reason,
        )
        self._emit_state()
        return self._state

    def restore_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        *,
        had_missing_configs: bool,
    ) -> FactoryControllerState:
        state = self.set_selection(selected_config_ids)
        if not had_missing_configs:
            return state
        self._restored_selection_has_missing_configs = True
        self._state = replace(
            state,
            selection_ready=False,
            selection_ready_reason="配置已失效",
            start_ready=False,
            start_ready_reason="配置已失效",
            run_phase="not_ready",
        )
        self._emit_state()
        return self._state

    def refresh_selection(self) -> FactoryControllerState:
        if bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            return self._state
        selected = tuple(self._state.selected_config_ids)
        config_service = getattr(self.services, "config_service", None)
        list_configs = getattr(config_service, "list_configs", None)
        if callable(list_configs):
            try:
                rows = tuple(list_configs())
            except Exception:
                rows = None
            if rows is not None:
                existing = {
                    row.get("config_id")
                    for row in rows
                    if type(row) is dict and type(row.get("config_id")) is int
                }
                pruned = tuple(
                    config_id for config_id in selected if config_id in existing
                )
                if pruned != selected:
                    selected = pruned
                    self._restored_selection_has_missing_configs = True
                    settings = getattr(
                        self.services,
                        "factory_selection_settings",
                        None,
                    )
                    save = getattr(settings, "save_selected_config_ids", None)
                    if callable(save):
                        try:
                            error = save(selected)
                        except Exception as exc:
                            error = str(exc)
                        if error:
                            self.bus.factory_selection_notice.emit(str(error))
        self._selected_configs = self._load_selected_configs(selected)
        ready, reason = self._selection_readiness(selected)
        self._state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if ready else "not_ready",
            selection_ready=ready,
            selection_ready_reason=reason,
            start_ready=ready,
            start_ready_reason=reason,
        )
        self._emit_state()
        return self._state

    def handle_configuration_changed(self) -> FactoryControllerState:
        if not bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            return self.refresh_selection()
        settings = getattr(
            self.services,
            "factory_selection_settings",
            None,
        )
        if not callable(
            getattr(settings, "save_pending_selected_config_ids", None)
        ):
            return self._state

        selected = tuple(self._state.selected_config_ids)
        config_service = getattr(self.services, "config_service", None)
        list_configs = getattr(config_service, "list_configs", None)
        if callable(list_configs):
            try:
                rows = tuple(list_configs())
            except Exception:
                rows = None
            if rows is not None:
                available = tuple(
                    row.get("config_id")
                    for row in rows
                    if type(row) is dict
                    and type(row.get("config_id")) is int
                    and row.get("config_id") > 0
                )
                available_set = set(available)
                selected = tuple(
                    config_id
                    for config_id in selected
                    if config_id in available_set
                )
                if not selected and available:
                    selected = (available[0],)
        return self._set_pending_selection(selected)

    def can_arm(self) -> bool:
        if bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            return self._state.start_ready
        ready, reason = self._selection_readiness(
            self._state.selected_config_ids
        )
        next_state = replace(
            self._state,
            run_phase="armed" if ready else "not_ready",
            selection_ready=ready,
            selection_ready_reason=reason,
            start_ready=ready,
            start_ready_reason=reason,
        )
        if next_state != self._state:
            self._state = next_state
            self._emit_state()
        return ready

    def start_manual(self, workorder: dict | None = None) -> bool:
        if not self.can_arm():
            return self._emit_blocked("manual")
        config_id = self._route_config_id(TriggerSource.MANUAL, None)
        if config_id is None:
            return False
        trigger_monotonic_ns = self._sample_trigger()
        if trigger_monotonic_ns is None:
            return self._emit_blocked_reason(
                "invalid captured-ingress timestamp",
                "manual",
            )
        return self._submit_captured(
            source=TriggerSource.MANUAL,
            trigger_monotonic_ns=trigger_monotonic_ns,
            workorder=workorder,
            plc_id=None,
            config_id=config_id,
        )

    def handle_plc_event(self, payload: FactoryPlcIngressPayload) -> bool:
        self._last_plc_ingress = payload
        if not self.can_arm():
            return self._emit_blocked("plc")
        config_id = self._route_config_id(TriggerSource.PLC, payload.plc_id)
        if config_id is None:
            return False
        workorder = {
            key: value
            for key, value in {
                "sn": payload.sn,
                "model": payload.model,
                "rotation_speed": payload.rotation_speed,
            }.items()
            if value is not None
        }
        return self._submit_captured(
            source=TriggerSource.PLC,
            trigger_monotonic_ns=payload.trigger_monotonic_ns,
            workorder=workorder,
            plc_id=payload.plc_id,
            config_id=config_id,
        )

    def handle_scan_event(self, payload: FactoryScanIngressPayload) -> bool:
        self._last_scan_ingress = payload
        if not self.can_arm():
            return self._emit_blocked("scanner")
        config_id = self._route_config_id(TriggerSource.SCANNER, None)
        if config_id is None:
            return False
        return self._submit_captured(
            source=TriggerSource.SCANNER,
            trigger_monotonic_ns=payload.trigger_monotonic_ns,
            workorder={"sn": payload.scan_text},
            plc_id=None,
            config_id=config_id,
        )

    def analyze_imported_audio(self, request) -> bool:
        coordinator = getattr(self.services, "multi_run_coordinator", None)
        start = getattr(coordinator, "start_imported", None)
        if not callable(start):
            return False
        try:
            result = start(request)
        except Exception:
            return False
        return getattr(result, "status", None) in _ACCEPTED_STARTS

    def apply_manual_judgment(self, _label: str) -> ManualLabelUpdateResult:
        return ManualLabelUpdateResult(
            False,
            "Use the selected Dashboard run for manual judgment.",
        )

    def begin_reanalysis(self, _start_sec: float, _end_sec: float) -> bool:
        return False

    def stop_active_run(self) -> bool:
        return False

    def clear_display_state(self, reason: str) -> None:
        self.clear_for_next_run(reason)

    def clear_for_next_run(self, reason: str = "next_run_accepted") -> None:
        self.bus.factory_display_reset.emit(reason)

    def _sample_trigger(self) -> int | None:
        try:
            value = self._monotonic_ns()
        except Exception:
            return None
        if type(value) is not int or value <= 0:
            return None
        return value

    def _reject_selection_mutation(self) -> bool:
        if not bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            return False
        self.bus.factory_selection_notice.emit(
            PRODUCTION_SELECTION_LEASE_NOTICE
        )
        return True

    def _set_prepared_selection(
        self,
        selected: tuple[int, ...],
    ) -> FactoryControllerState:
        prepared_ids = {
            config_id
            for config_id, _plc_id in self._prepared_config_routes
        }
        if any(type(config_id) is not int for config_id in selected) or (
            len(set(selected)) != len(selected)
        ):
            self._reject_selection_mutation()
            return self._state
        if self._selection_restart_required or any(
            config_id not in prepared_ids for config_id in selected
        ):
            return self._set_pending_selection(selected)
        ready = bool(selected)
        reason = None if ready else "请至少启用一个配置"
        self._restored_selection_has_missing_configs = False
        self._selected_configs = {
            config_id: {"config_id": config_id}
            for config_id in selected
        }
        self._state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if ready else "not_ready",
            selection_ready=ready,
            selection_ready_reason=reason,
            start_ready=ready,
            start_ready_reason=reason,
        )
        self._emit_state()
        return self._state

    def _set_pending_selection(
        self,
        selected: tuple[int, ...],
    ) -> FactoryControllerState:
        if (
            any(
                type(config_id) is not int or config_id <= 0
                for config_id in selected
            )
            or len(set(selected)) != len(selected)
        ):
            self._reject_selection_mutation()
            return self._state

        settings = getattr(
            self.services,
            "factory_selection_settings",
            None,
        )
        save = getattr(settings, "save_pending_selected_config_ids", None)
        if not callable(save):
            self._reject_selection_mutation()
            return self._state
        try:
            error = save(selected)
        except Exception:
            error = "下次启动配置保存失败"
        if error:
            self.bus.factory_selection_notice.emit(str(error))
            return self._state

        self._selection_restart_required = True
        self._restored_selection_has_missing_configs = False
        self._selected_configs = {
            config_id: {"config_id": config_id}
            for config_id in selected
        }
        self._state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase=(
                self._state.run_phase
                if self._state.run_phase
                in {"running", "analyzing", "reanalyzing"}
                else "not_ready"
            ),
            selection_ready=bool(selected),
            selection_ready_reason=(
                None if selected else "请至少启用一个配置"
            ),
            start_ready=False,
            start_ready_reason=PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
        )
        self._emit_state()
        self.bus.factory_selection_notice.emit(
            PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE
        )
        return self._state

    def _submit_captured(
        self,
        *,
        source: TriggerSource,
        trigger_monotonic_ns: int,
        workorder: dict | None,
        plc_id: int | None,
        config_id: int,
    ) -> bool:
        submit = getattr(self.services, "submit_captured_start", None)
        if callable(submit):
            try:
                return bool(
                    submit(
                        source=source,
                        trigger_monotonic_ns=trigger_monotonic_ns,
                        workorder=workorder,
                        plc_id=plc_id,
                        config_id=config_id,
                    )
                )
            except Exception:
                return False
        build = getattr(self.services, "build_captured_start_request", None)
        coordinator = getattr(self.services, "multi_run_coordinator", None)
        start = getattr(coordinator, "start_captured", None)
        if not callable(build) or not callable(start):
            return False
        try:
            request = build(
                source=source,
                trigger_monotonic_ns=trigger_monotonic_ns,
                workorder=workorder,
                plc_id=plc_id,
                config_id=config_id,
            )
            result = start(request)
        except Exception:
            return False
        return getattr(result, "status", None) in _ACCEPTED_STARTS

    @staticmethod
    def _normalize_routes(
        routes: tuple[tuple[int, int | None], ...],
    ) -> tuple[tuple[int, int | None], ...]:
        if type(routes) is not tuple:
            raise ValueError("prepared config routes must be an exact tuple")
        normalized = []
        for route in routes:
            if type(route) is not tuple or len(route) != 2:
                raise ValueError("prepared config route is malformed")
            config_id, plc_id = route
            if type(config_id) is not int or config_id <= 0:
                raise ValueError("prepared config route ID is invalid")
            if plc_id is not None and (
                type(plc_id) is not int or plc_id <= 0
            ):
                raise ValueError("prepared PLC route ID is invalid")
            normalized.append((config_id, plc_id))
        if len({config_id for config_id, _plc_id in normalized}) != len(
            normalized
        ):
            raise ValueError("prepared config routes contain duplicate IDs")
        return tuple(normalized)

    def _routes(self) -> tuple[tuple[int, int | None], ...]:
        if bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            selected = self._state.selected_config_ids
            route_by_config_id = {
                config_id: (config_id, plc_id)
                for config_id, plc_id in self._prepared_config_routes
            }
            if selected and selected[0] not in route_by_config_id:
                return ()
            return tuple(
                route_by_config_id[config_id]
                for config_id in selected
                if config_id in route_by_config_id
            )
        routes = []
        for config_id in self._state.selected_config_ids:
            config = self._selected_configs.get(config_id, {})
            raw_plc_id = config.get("plc_id")
            plc_id = (
                raw_plc_id
                if type(raw_plc_id) is int and raw_plc_id > 0
                else None
            )
            routes.append((config_id, plc_id))
        return tuple(routes)

    def _route_config_id(
        self,
        source: TriggerSource,
        plc_id: int | None,
    ) -> int | None:
        routes = self._routes()
        if source is TriggerSource.PLC:
            if type(plc_id) is not int or plc_id <= 0:
                return self._reject_route(
                    "PLC ingress requires a positive integer PLC ID."
                )
            matches = tuple(
                config_id
                for config_id, route_plc_id in routes
                if route_plc_id == plc_id
            )
            if matches:
                return matches[0]
            # A registered PLC does not have to participate in production.
            # Its ingress adapter still resets the start signal; there is
            # simply no run to create when no selected config uses this PLC.
            return None
        if source is TriggerSource.MANUAL:
            if routes:
                return routes[0][0]
            return self._reject_route(
                "manual ingress requires at least one prepared config."
            )
        if len(routes) == 1:
            return routes[0][0]
        return self._reject_route(
            f"{source.name.lower()} ingress requires exactly one "
            "prepared config."
        )

    def _reject_route(self, reason: str) -> None:
        self.bus.factory_selection_notice.emit(reason)
        log = getattr(self.bus, "log", None)
        if log is not None:
            log.emit(reason)
        return None

    def _load_selected_configs(
        self,
        selected: tuple[int, ...],
    ) -> dict[int, dict]:
        config_service = getattr(self.services, "config_service", None)
        load = getattr(config_service, "load_config", None)
        if not callable(load):
            return {config_id: {"config_id": config_id} for config_id in selected}
        configs = {}
        for config_id in selected:
            try:
                config = load(config_id)
            except Exception:
                return {}
            if type(config) is not dict:
                return {}
            configs[config_id] = config
        return configs

    def _selection_readiness(
        self,
        selected: tuple[int, ...],
    ) -> tuple[bool, str | None]:
        if not selected:
            return False, "请至少启用一个配置"
        if self._restored_selection_has_missing_configs:
            return False, "配置已失效"
        if len(self._selected_configs) != len(selected):
            return False, "配置加载失败"
        validate = getattr(
            getattr(self.services, "config_service", None),
            "validate_factory_runtime",
            None,
        )
        if callable(validate):
            for config in self._selected_configs.values():
                try:
                    errors = tuple(validate(config))
                except Exception:
                    return False, "配置校验失败"
                if errors:
                    return False, str(errors[0])
        return True, None

    def _emit_blocked(self, trigger_source: str) -> bool:
        return self._emit_blocked_reason(
            self._state.start_ready_reason or "factory ingress is not ready",
            trigger_source,
        )

    def _emit_blocked_reason(self, reason: str, trigger_source: str) -> bool:
        self.bus.factory_start_blocked.emit(
            FactoryStartBlockedPayload(
                reason=reason,
                trigger_source=trigger_source,
            )
        )
        return False

    def _connect_bus(self) -> None:
        if hasattr(self.bus, "config_changed"):
            self.bus.config_changed.connect(
                self.handle_configuration_changed
            )
        if hasattr(self.bus, "plc_changed"):
            self.bus.plc_changed.connect(self.refresh_selection)
        self.bus.factory_plc_event.connect(self.handle_plc_event)
        self.bus.factory_scan_event.connect(self.handle_scan_event)

    def _emit_state(self) -> None:
        self.bus.factory_state_changed.emit(self._state)
