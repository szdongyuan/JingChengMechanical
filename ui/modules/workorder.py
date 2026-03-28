from __future__ import annotations

from PyQt6.QtWidgets import QFormLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState
from ui.widgets.factory_config_list import FactoryConfigListWidget


class WorkOrderModule(QWidget):
    module_id = "workorder"
    _LOCKED_SELECTION_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _PENDING_START_REASON = "启动请求处理中"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._config_rows: list[dict] = []
        self._config_details: dict[int, dict] = {}
        self._last_workorder_payload: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.ed_sn = QLineEdit()
        self.ed_sn.setPlaceholderText("请输入条码")
        self.ed_model = QLineEdit()
        self.ed_model.setPlaceholderText("请输入产品型号")
        self.ed_cfg = QLineEdit()
        self.ed_cfg.setPlaceholderText("Factory priority controlled")
        self.ed_cfg.setReadOnly(True)
        self.ed_cfg.setEnabled(False)

        self.config_selector = FactoryConfigListWidget()
        self.config_selector.setMinimumHeight(180)

        self.lbl_persistence_notice = QLabel()
        self.lbl_persistence_notice.setWordWrap(True)
        self.lbl_persistence_notice.setStyleSheet("color: #b45309;")
        self.lbl_persistence_notice.setVisible(False)
        self.lbl_selection_status = QLabel()
        self.lbl_selection_status.setWordWrap(True)
        self.lbl_phase_summary = QLabel()
        self.lbl_phase_summary.setWordWrap(True)
        self.lbl_trigger_summary = QLabel()
        self.lbl_trigger_summary.setWordWrap(True)
        self.lbl_factory_summary = QLabel()
        self.lbl_factory_summary.setWordWrap(True)

        form = QFormLayout()
        form.addRow("条码 SN", self.ed_sn)
        form.addRow("产品型号 Model", self.ed_model)
        form.addRow("测试配置 Config", self.ed_cfg)
        form.addRow("工厂配置 Factory Configs", self.config_selector)
        layout.addLayout(form)

        layout.addWidget(self.lbl_persistence_notice)
        layout.addWidget(self.lbl_selection_status)
        layout.addWidget(self.lbl_phase_summary)
        layout.addWidget(self.lbl_trigger_summary)
        layout.addWidget(self.lbl_factory_summary)

        self.ed_sn.textChanged.connect(lambda _text: self._publish_workorder())
        self.ed_model.textChanged.connect(lambda _text: self._publish_workorder())
        self.config_selector.enabled_order_changed.connect(self._on_config_selection_changed)
        if hasattr(self.services.bus, "config_changed"):
            self.services.bus.config_changed.connect(self._reload_config_options)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_selection_notice"):
            self.services.bus.factory_selection_notice.connect(self._on_factory_selection_notice)

        self._reload_config_options()
        self._restore_startup_selection()
        initial_payload = self.value()
        self._last_workorder_payload = dict(initial_payload)
        self._cache_current_workorder_payload(initial_payload)

    def value(self) -> dict:
        return {
            "sn": self.ed_sn.text().strip(),
            "model": self.ed_model.text().strip(),
            "selected_config_ids": self.config_selector.enabled_config_ids(),
        }

    def apply(self):
        self._publish_workorder(force=True, log=True)

    def _on_config_selection_changed(self, selected_config_ids: tuple[int, ...]) -> None:
        next_state = None
        factory_mode = getattr(self.services, "factory_mode", None)
        set_selection = getattr(factory_mode, "set_selection", None)
        if callable(set_selection):
            next_state = set_selection(selected_config_ids)
        if isinstance(next_state, FactoryControllerState):
            self._sync_factory_state(next_state, publish_workorder=False)
        else:
            self._update_selection_status()
            self._update_phase_trigger_summary()
            self._update_cfg_display()
            self._update_summary_text()
            self._sync_selector_lock_state()
        self._publish_workorder()
        self._persist_selected_config_ids(selected_config_ids)

    def _on_factory_state_changed(self, state) -> None:
        if isinstance(state, FactoryControllerState):
            self._sync_factory_state(state)

    def _sync_factory_state(
        self,
        state: FactoryControllerState,
        *,
        publish_workorder: bool = True,
    ) -> None:
        self._factory_state = state
        self.config_selector.sync_enabled_config_ids(state.selected_config_ids)
        self._update_selection_status()
        self._update_phase_trigger_summary()
        self._update_cfg_display()
        self._update_summary_text()
        self._sync_selector_lock_state()
        if publish_workorder:
            self._publish_workorder()

    def _restore_startup_selection(self) -> None:
        startup_selection = self._load_startup_selection()
        available_ids = self._available_config_ids()
        restored_selection = tuple(
            config_id for config_id in startup_selection if config_id in available_ids
        )
        had_missing_configs = restored_selection != startup_selection

        self.config_selector.sync_enabled_config_ids(restored_selection)

        factory_mode = getattr(self.services, "factory_mode", None)
        next_state = None
        restore_selection = getattr(factory_mode, "restore_selection", None)
        if callable(restore_selection):
            next_state = restore_selection(
                restored_selection,
                had_missing_configs=had_missing_configs,
            )
        else:
            set_selection = getattr(factory_mode, "set_selection", None)
            if callable(set_selection):
                next_state = set_selection(restored_selection)
        if isinstance(next_state, FactoryControllerState):
            self._sync_factory_state(next_state, publish_workorder=False)
        else:
            self._update_selection_status()
            self._update_phase_trigger_summary()
            self._update_cfg_display()
            self._update_summary_text()
            self._sync_selector_lock_state()

    def _load_startup_selection(self) -> tuple[int, ...]:
        selection_settings = getattr(self.services, "factory_selection_settings", None)
        load_selected_config_ids = getattr(selection_settings, "load_selected_config_ids", None)
        if callable(load_selected_config_ids):
            try:
                return tuple(load_selected_config_ids())
            except Exception:
                return ()
        return tuple(self._factory_state.selected_config_ids)

    def _persist_selected_config_ids(self, selected_config_ids: tuple[int, ...]) -> None:
        selection_settings = getattr(self.services, "factory_selection_settings", None)
        save_selected_config_ids = getattr(selection_settings, "save_selected_config_ids", None)
        error_message = None
        if callable(save_selected_config_ids):
            error_message = save_selected_config_ids(tuple(selected_config_ids))
        self._emit_selection_notice(error_message or "")

    def _emit_selection_notice(self, message: str) -> None:
        notice_signal = getattr(self.services.bus, "factory_selection_notice", None)
        emit = getattr(notice_signal, "emit", None)
        if callable(emit):
            emit(message)
            return
        self._on_factory_selection_notice(message)

    def _on_factory_selection_notice(self, message: str) -> None:
        self.lbl_persistence_notice.setText(message)
        self.lbl_persistence_notice.setVisible(bool(message))

    def _reload_config_options(self) -> None:
        self._config_rows = self._list_config_rows()
        self._config_details = {}
        visible_selection = self.config_selector.enabled_config_ids()
        if not visible_selection:
            visible_selection = tuple(self._factory_state.selected_config_ids)
        available_ids = self._available_config_ids()
        self.config_selector.set_rows(
            rows=self._config_rows,
            enabled_config_ids=tuple(
                config_id for config_id in visible_selection if config_id in available_ids
            ),
        )
        self._update_cfg_display()
        self._update_summary_text()
        self._sync_selector_lock_state()

    def _available_config_ids(self) -> set[int]:
        return {
            row.get("config_id")
            for row in self._config_rows
            if isinstance(row.get("config_id"), int)
        }

    def _list_config_rows(self) -> list[dict]:
        config_service = getattr(self.services, "config_service", None)
        list_configs = getattr(config_service, "list_configs", None)
        if not callable(list_configs):
            return []
        try:
            return list(list_configs())
        except Exception:
            return []

    def _sync_selector_lock_state(self) -> None:
        is_locked = (
            self._factory_state.run_phase in self._LOCKED_SELECTION_PHASES
            or self._factory_state.start_ready_reason == self._PENDING_START_REASON
        )
        self.config_selector.set_locked(is_locked)

    def _update_selection_status(self) -> None:
        if self._factory_state.selection_ready:
            text = "Factory selection: 已就绪"
        else:
            reason = self._factory_state.selection_ready_reason or "工厂模式未就绪"
            text = f"Factory selection: {reason}"
        self.lbl_selection_status.setText(text)

    def _update_phase_trigger_summary(self) -> None:
        run_phase = self._factory_state.run_phase or "not_ready"
        trigger_source = self._factory_state.trigger_source or "-"
        self.lbl_phase_summary.setText(f"Current phase: {run_phase}")
        self.lbl_trigger_summary.setText(f"Current/last trigger: {trigger_source}")

    def _update_cfg_display(self) -> None:
        primary_config_id = self.config_selector.primary_config_id()
        if primary_config_id is None:
            self.ed_cfg.setText("Factory priority list controlled")
            return
        self.ed_cfg.setText(self._config_name(primary_config_id))

    def _update_summary_text(self) -> None:
        selected_config_ids = self.config_selector.enabled_config_ids()
        if not selected_config_ids:
            self.lbl_factory_summary.setText("Factory summary: 请至少启用一个配置。")
            return

        lines = []
        for index, config_id in enumerate(selected_config_ids, start=1):
            config = self._config_detail(config_id)
            if not config:
                continue
            prefix = f"Priority {index}"
            if index == 1:
                prefix += " (Primary)"
            lines.append(
                (
                    f"{prefix}: {self._config_name(config_id)} | "
                    f"Scan: {self._scan_summary(config)} | "
                    f"PLC: {self._plc_summary(config)} | "
                    f"Channels: {self._channels_summary(config)} | "
                    f"Record: {self._duration_text(config.get('record_duration'))} | "
                    f"Analysis: {self._duration_text(config.get('analysis_duration'))}"
                )
            )
        self.lbl_factory_summary.setText("\n".join(lines))

    def _config_name(self, config_id: int) -> str:
        for row in self._config_rows:
            if row.get("config_id") == config_id:
                return row.get("config_name") or f"Config {config_id}"
        detail = self._config_detail(config_id)
        return detail.get("config_name") or f"Config {config_id}"

    def _config_detail(self, config_id: int) -> dict:
        if config_id in self._config_details:
            return self._config_details[config_id]

        detail = {}
        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if callable(load_config):
            try:
                detail = dict(load_config(config_id))
            except Exception:
                detail = {}
        detail.setdefault("config_id", config_id)
        detail.setdefault("config_name", self._config_name_fallback(config_id))
        self._config_details[config_id] = detail
        return detail

    def _config_name_fallback(self, config_id: int) -> str:
        for row in self._config_rows:
            if row.get("config_id") == config_id:
                return row.get("config_name") or f"Config {config_id}"
        return f"Config {config_id}"

    @staticmethod
    def _plc_summary(config: dict) -> str:
        return config.get("plc_device_label") or config.get("plc_device_key") or "-"

    @staticmethod
    def _scan_summary(config: dict) -> str:
        return "enabled" if bool(config.get("scan_triggers_test")) else "disabled"

    @staticmethod
    def _channels_summary(config: dict) -> str:
        channels = config.get("selected_channels") or []
        if not channels:
            return "-"
        return ", ".join(f"CH{channel_index + 1}" for channel_index in channels)

    @staticmethod
    def _duration_text(value) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.1f}s"
        return "-"

    def _publish_workorder(self, *, force: bool = False, log: bool = False) -> None:
        payload = self.value()
        self._cache_current_workorder_payload(payload)
        if not force and payload == self._last_workorder_payload:
            return

        self._last_workorder_payload = dict(payload)
        self.services.bus.workorder_changed.emit(payload)
        if log:
            self.services.bus.log.emit(f"[WorkOrder] applied {payload}")

    def _cache_current_workorder_payload(self, payload: dict) -> None:
        self.services.bus.current_workorder_payload = dict(payload)
