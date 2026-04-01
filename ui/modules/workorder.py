from __future__ import annotations

from PyQt6.QtWidgets import QFormLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState
from ui.widgets.factory_config_list import FactoryConfigListWidget


class WorkOrderModule(QWidget):
    module_id = "workorder"
    _LOCKED_SELECTION_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _PENDING_START_REASON = "启动请求处理中"
    _PHASE_LABELS = {
        "not_ready": "未就绪",
        "armed": "待启动",
        "running": "录音中",
        "analyzing": "分析中",
        "reviewing": "等待复核",
        "reanalyzing": "重新分析中",
    }
    _TRIGGER_LABELS = {
        "manual": "手动",
        "plc": "PLC",
        "scan": "扫码",
    }

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._config_rows: list[dict] = []
        self._last_workorder_payload: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.ed_sn = QLineEdit()
        self.ed_sn.setPlaceholderText("请输入条码")
        self.ed_model = QLineEdit()
        self.ed_model.setPlaceholderText("请输入产品型号")

        self.config_selector = FactoryConfigListWidget()
        self.config_selector.setMinimumHeight(180)
        self.lbl_factory_configs = QLabel("配置选择")

        self.lbl_persistence_notice = QLabel()
        self.lbl_persistence_notice.setWordWrap(True)
        self.lbl_persistence_notice.setStyleSheet("color: #b45309;")
        self.lbl_persistence_notice.setVisible(False)
        self.lbl_selection_status = QLabel()
        self.lbl_selection_status.setWordWrap(True)
        self.lbl_selection_status.setVisible(False)
        self.lbl_phase_summary = QLabel()
        self.lbl_phase_summary.setWordWrap(True)
        self.lbl_trigger_summary = QLabel()
        self.lbl_trigger_summary.setWordWrap(True)

        self.form_layout = QFormLayout()
        self.form_layout.addRow("条码 SN", self.ed_sn)
        self.form_layout.addRow("产品型号", self.ed_model)
        layout.addLayout(self.form_layout)

        self.factory_config_section = QWidget()
        factory_config_layout = QVBoxLayout(self.factory_config_section)
        factory_config_layout.setContentsMargins(0, 0, 0, 0)
        factory_config_layout.setSpacing(6)
        factory_config_layout.addWidget(self.lbl_factory_configs)
        factory_config_layout.addWidget(self.config_selector)
        layout.addWidget(self.factory_config_section)

        layout.addWidget(self.lbl_persistence_notice)
        layout.addWidget(self.lbl_selection_status)
        layout.addWidget(self.lbl_phase_summary)
        layout.addWidget(self.lbl_trigger_summary)

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
        restored_selection = self._dedupe_config_ids(restored_selection)

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

    @staticmethod
    def _dedupe_config_ids(config_ids: tuple[int, ...] | list[int]) -> tuple[int, ...]:
        seen: set[int] = set()
        deduped: list[int] = []
        for config_id in config_ids:
            if not isinstance(config_id, int) or config_id in seen:
                continue
            seen.add(config_id)
            deduped.append(config_id)
        return tuple(deduped)

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
        # ControlModule owns controller blocking feedback near the start button.
        self.lbl_selection_status.clear()
        self.lbl_selection_status.setVisible(False)

    def _update_phase_trigger_summary(self) -> None:
        display_phase = self._display_phase(self._factory_state.run_phase)
        display_trigger = self._display_trigger(self._factory_state.trigger_source)
        self.lbl_phase_summary.setText(f"当前阶段：{display_phase}")
        self.lbl_trigger_summary.setText(f"最近触发方式：{display_trigger}")

    def _display_phase(self, run_phase: str | None) -> str:
        phase = run_phase or "not_ready"
        return self._PHASE_LABELS.get(phase, phase)

    def _display_trigger(self, trigger_source: str | None) -> str:
        if not trigger_source:
            return "-"
        return self._TRIGGER_LABELS.get(trigger_source, trigger_source)

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
