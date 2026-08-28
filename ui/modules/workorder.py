from __future__ import annotations

from PyQt6.QtWidgets import QGridLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState
from model.plc_table import PlcTable
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
        "stopped": "已停止",
    }

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._config_rows: list[dict] = []
        self._last_workorder_payload: dict | None = None
        self._plc_sn_values: dict[int, str] = {}
        self._plc_sn_edits: dict[int, QLineEdit] = {}
        self._plc_model_values: dict[int, str] = {}
        self._plc_model_edits: dict[int, QLineEdit] = {}
        self._rendered_plc_ids: tuple[int, ...] = ()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.plc_sn_widget = QWidget()
        self.plc_sn_layout = QGridLayout(self.plc_sn_widget)
        self.plc_sn_layout.setContentsMargins(0, 0, 2, 0)
        self.plc_sn_layout.setHorizontalSpacing(8)
        self.plc_sn_layout.setVerticalSpacing(6)
        self.plc_sn_layout.setColumnStretch(0, 1)
        self.plc_sn_layout.setColumnStretch(1, 1)

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

        layout.addWidget(self.plc_sn_widget)

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

        self.config_selector.enabled_order_changed.connect(self._on_config_selection_changed)
        if hasattr(self.services.bus, "config_changed"):
            self.services.bus.config_changed.connect(self._reload_config_options)
        if hasattr(self.services.bus, "plc_changed"):
            self.services.bus.plc_changed.connect(self._refresh_plc_sn_rows)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_selection_notice"):
            self.services.bus.factory_selection_notice.connect(self._on_factory_selection_notice)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(self.services.bus, "plc_workorder_changed"):
            self.services.bus.plc_workorder_changed.connect(self._on_plc_workorder_changed)

        self._reload_config_options()
        self._refresh_plc_sn_rows()
        self._restore_startup_selection()
        initial_payload = self.value()
        self._last_workorder_payload = dict(initial_payload)
        self._cache_current_workorder_payload(initial_payload)

    def value(self) -> dict:
        return {
            "sn": "",
            "model": "",
            "selected_config_ids": self.config_selector.enabled_config_ids(),
        }

    def apply(self):
        self._publish_workorder(force=True, log=True)

    def _on_config_selection_changed(self, selected_config_ids: tuple[int, ...]) -> None:
        previous_selected = set(self._factory_state.selected_config_ids)
        added_config_ids = tuple(
            config_id
            for config_id in selected_config_ids
            if config_id not in previous_selected
        )
        request_resets = getattr(
            self.services,
            "request_plc_start_signal_resets_for_configs",
            None,
        )
        if added_config_ids and callable(request_resets):
            request_resets(added_config_ids)

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

    def _on_factory_display_reset(self, reason: str) -> None:
        if reason not in {"manual_judgment_ok", "manual_judgment_ng"}:
            return
        self._publish_workorder(force=True)

    def _on_plc_workorder_changed(self, payload: dict) -> None:
        sn = str(payload.get("sn") or "").strip()
        model = str(payload.get("model") or "").strip()
        plc_id = self._payload_plc_id(payload)
        if plc_id is None:
            return
        self._plc_sn_values[plc_id] = sn
        self._plc_model_values[plc_id] = model
        self._refresh_plc_sn_rows()
        for other_edit in self._plc_sn_edits.values():
            other_edit.setStyleSheet("")
        edit = self._plc_sn_edits.get(plc_id)
        if edit is not None and sn:
            edit.setStyleSheet("color: #2563eb; font-weight: 600;")

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
        request_resets = getattr(
            self.services,
            "request_plc_start_signal_resets_for_configs",
            None,
        )
        if callable(request_resets):
            request_resets(restored_selection)
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
        if tuple(self._factory_state.selected_config_ids) != tuple(
            selected_config_ids
        ):
            return
        if bool(
            getattr(
                getattr(self.services, "factory_mode", None),
                "selection_restart_required",
                False,
            )
        ):
            return
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
        self._refresh_plc_sn_rows()

    def _payload_plc_id(self, payload: dict) -> int | None:
        raw_plc_id = payload.get("plc_id")
        if isinstance(raw_plc_id, bool):
            return None
        if isinstance(raw_plc_id, int) and raw_plc_id > 0:
            return raw_plc_id
        try:
            plc_id = int(str(raw_plc_id).strip())
        except (TypeError, ValueError):
            return None
        return plc_id if plc_id > 0 else None

    def _refresh_plc_sn_rows(self) -> None:
        plc_ids = self._plc_ids_for_display()
        if plc_ids != self._rendered_plc_ids:
            self._rebuild_plc_sn_rows(plc_ids)
        for plc_id, edit in self._plc_sn_edits.items():
            edit.setText(self._plc_sn_values.get(plc_id, ""))
        for plc_id, edit in self._plc_model_edits.items():
            edit.setText(self._plc_model_values.get(plc_id, ""))

    def _plc_ids_for_display(self) -> tuple[int, ...]:
        plc_ids = self._known_plc_ids()
        if plc_ids:
            self._plc_sn_values = {
                plc_id: sn
                for plc_id, sn in self._plc_sn_values.items()
                if plc_id in plc_ids
            }
            self._plc_model_values = {
                plc_id: model
                for plc_id, model in self._plc_model_values.items()
                if plc_id in plc_ids
            }
            return tuple(sorted(plc_ids))
        return tuple(sorted(self._plc_sn_values))

    def _known_plc_ids(self) -> set[int]:
        plc_ids: set[int] = set()
        for attr_name in ("plc_services", "plc_threads", "plc_state"):
            mapping = getattr(self.services, attr_name, None)
            keys = getattr(mapping, "keys", None)
            if not callable(keys):
                continue
            for raw_id in keys():
                if isinstance(raw_id, bool):
                    continue
                if isinstance(raw_id, int) and raw_id > 0:
                    plc_ids.add(raw_id)

        try:
            rows = PlcTable.select(PlcTable.id).order_by(PlcTable.id)
            for row in rows:
                plc_id = getattr(row, "id", None)
                if isinstance(plc_id, int) and plc_id > 0:
                    plc_ids.add(plc_id)
        except Exception:
            pass

        return plc_ids

    def _rebuild_plc_sn_rows(self, plc_ids: tuple[int, ...]) -> None:
        while self.plc_sn_layout.count():
            item = self.plc_sn_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._plc_sn_edits = {}
        self._plc_model_edits = {}
        self._rendered_plc_ids = tuple(plc_ids)
        if not plc_ids:
            empty_label = QLabel("未配置 PLC")
            empty_label.setObjectName("Muted")
            self.plc_sn_layout.addWidget(empty_label, 0, 0, 1, 2)
            return

        for index, plc_id in enumerate(plc_ids):
            row_widget = QWidget()
            row_layout = QGridLayout(row_widget)
            row_layout.setContentsMargins(0, 1, 2, 1)
            row_layout.setSpacing(6)

            sn_label = QLabel(f"PLC{plc_id} SN码")
            sn_label.setFixedWidth(72)
            model_label = QLabel(f"PLC{plc_id} 型号")
            model_label.setFixedWidth(72)

            sn_edit = QLineEdit()
            sn_edit.setReadOnly(True)
            sn_edit.setPlaceholderText("未读取")
            sn_edit.setMinimumWidth(0)
            sn_edit.setMinimumHeight(28)
            sn_edit.setMaximumHeight(30)

            model_edit = QLineEdit()
            model_edit.setReadOnly(True)
            model_edit.setPlaceholderText("未读取")
            model_edit.setMinimumWidth(0)
            model_edit.setMinimumHeight(28)
            model_edit.setMaximumHeight(30)

            row_layout.addWidget(sn_label, 0, 0)
            row_layout.addWidget(sn_edit, 0, 1)
            row_layout.addWidget(model_label, 1, 0)
            row_layout.addWidget(model_edit, 1, 1)
            row_layout.setColumnStretch(1, 1)
            self.plc_sn_layout.addWidget(row_widget, index // 2, index % 2)
            self._plc_sn_edits[plc_id] = sn_edit
            self._plc_model_edits[plc_id] = model_edit

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
        self.lbl_phase_summary.setText(f"当前阶段：{display_phase}")

    def _display_phase(self, run_phase: str | None) -> str:
        phase = run_phase or "not_ready"
        return self._PHASE_LABELS.get(phase, phase)

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
