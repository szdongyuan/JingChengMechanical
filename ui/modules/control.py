from __future__ import annotations

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.service.factory_selection_settings import (
    PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
)
from app.service.multi_run_coordinator_mvp import OperationResult, OperationStatus
from app.service.result_history_contracts import ManualJudgmentTarget
from app.services_container import FactoryControllerState
from ui.popups.import_audio_analysis_dialog import ImportAudioAnalysisDialog


class ControlModule(QWidget):
    manual_judgment_intent = pyqtSignal(str, object)
    stop_intent = pyqtSignal()
    restart_intent = pyqtSignal()

    module_id = "control"
    _BUSY_RUN_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _STOP_ENABLED_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _REVIEW_CLEANUP_BUSY_REASON = "当前测试运行中"
    _PENDING_START_REASON = "启动请求处理中"
    _BUSY_START_READY_REASONS = frozenset(
        {
            _REVIEW_CLEANUP_BUSY_REASON,
            _PENDING_START_REASON,
            PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
        }
    )
    _BLOCKED_START_DIALOG_DELAY_MS = 180

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._workorder = dict(getattr(self.services.bus, "current_workorder_payload", {}) or {})
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._blocked_start_dialog = None
        self._blocked_start_dialog_source: str | None = None
        self._pending_blocked_start_reason: str | None = None
        self._pending_blocked_start_source: str | None = None
        self._manual_judgment_target: ManualJudgmentTarget | None = None
        self._configured_fault_types = self._load_configured_fault_types()
        self._selected_manual_fault_type: str | None = None
        self._manual_intent_key: tuple[str, int] | None = None
        self._manual_fault_buttons: list[QPushButton] = []
        self._rendered_manual_button_key: (
            tuple[str | None, tuple[str, ...]] | None
        ) = None
        self._restart_pending = False
        self._blocked_start_dialog_timer = QTimer(self)
        self._blocked_start_dialog_timer.setSingleShot(True)
        self._blocked_start_dialog_timer.timeout.connect(
            self._open_pending_blocked_start_dialog
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        row1 = QHBoxLayout()
        self.btn_start = QPushButton("▷ 自动开始")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setMinimumHeight(36)
        self.btn_stop = QPushButton("停止测试")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setMinimumHeight(36)
        row1.addWidget(self.btn_start)
        row1.addWidget(self.btn_stop)
        lay.addLayout(row1)

        self.manual_judgment_widget = QWidget(self)
        self.manual_judgment_widget.setObjectName("manual_judgment_actions")
        judgment_section = QVBoxLayout(self.manual_judgment_widget)
        judgment_section.setContentsMargins(0, 4, 0, 0)
        judgment_section.setSpacing(6)
        self.manual_fault_button_layout = QGridLayout()
        self.manual_fault_button_layout.setContentsMargins(0, 0, 0, 0)
        self.manual_fault_button_layout.setHorizontalSpacing(6)
        self.manual_fault_button_layout.setVerticalSpacing(6)
        self.manual_fault_button_group = QButtonGroup(self)
        self.manual_fault_button_group.setExclusive(True)
        judgment_section.addLayout(self.manual_fault_button_layout)

        self.btn_pass = QPushButton("√ 合格", self.manual_judgment_widget)
        self.btn_pass.setObjectName("Success")
        self.btn_pass.setMinimumHeight(36)
        self.btn_fail = QPushButton("× 不合格", self.manual_judgment_widget)
        self.btn_fail.setObjectName("Fail")
        self.btn_fail.setMinimumHeight(36)
        self.manual_judgment_widget.hide()

        row3 = QHBoxLayout()
        self.btn_import_audio = QPushButton("导入音频分析")
        self.btn_import_audio.setMinimumHeight(36)
        self.btn_restart = QPushButton("重启软件")
        self.btn_restart.setMinimumHeight(36)
        row3.addWidget(self.btn_import_audio)
        row3.addWidget(self.btn_restart)
        lay.addLayout(row3)

        self.lbl_production_unavailable_reason = QLabel()
        self.lbl_production_unavailable_reason.setWordWrap(True)
        self.lbl_production_unavailable_reason.setStyleSheet("color: #b45309;")
        production_reason = self._production_unavailable_reason()
        self.lbl_production_unavailable_reason.setText(production_reason or "")
        self.lbl_production_unavailable_reason.setVisible(bool(production_reason))
        lay.addWidget(self.lbl_production_unavailable_reason)

        self.services.bus.workorder_changed.connect(self._on_workorder)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_start_blocked"):
            self.services.bus.factory_start_blocked.connect(self._on_factory_start_blocked)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_pass.clicked.connect(
            lambda: self._emit_manual_judgment_intent("OK")
        )
        self.btn_fail.clicked.connect(
            lambda: self._emit_manual_judgment_intent("NG")
        )
        self.btn_import_audio.clicked.connect(self._import_audio)
        self.btn_restart.clicked.connect(self._request_restart)
        self._render_manual_fault_buttons()
        self._sync_manual_judgment_controls()
        self._sync_start_state()

    def _on_workorder(self, wo: dict):
        self._workorder = wo or {}
        self._sync_start_state()

    def _on_factory_state_changed(self, state):
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
            configured = self._load_configured_fault_types()
            if configured != self._configured_fault_types:
                self._configured_fault_types = configured
                if self._manual_judgment_target is None:
                    self._selected_manual_fault_type = None
                    self._render_manual_fault_buttons()
            self._sync_start_state()
            self._sync_blocked_start_dialog()

    def _on_factory_start_blocked(self, payload) -> None:
        reason = getattr(payload, "reason", None)
        if not isinstance(reason, str):
            return
        reason = reason.strip()
        if not reason:
            return
        if reason in self._BUSY_START_READY_REASONS:
            return
        self._show_or_update_blocked_start_dialog(reason, source="blocked_start")

    def _start(self):
        if not self.btn_start.isEnabled():
            return
        factory_mode = getattr(self.services, "factory_mode", None)
        start_manual = getattr(factory_mode, "start_manual", None)
        if callable(start_manual):
            start_manual(self._workorder)

    def _stop(self):
        self.stop_intent.emit()

    def _request_restart(self) -> None:
        if self._restart_pending:
            return
        self.set_restart_pending(True)
        self.restart_intent.emit()

    def set_restart_pending(self, pending: bool) -> None:
        self._restart_pending = bool(pending)
        self.btn_restart.setEnabled(not self._restart_pending)

    def _import_audio(self) -> None:
        if self._import_audio_disabled():
            return

        factory_mode = getattr(self.services, "factory_mode", None)
        state = getattr(factory_mode, "state", self._factory_state)
        selected_config_ids = tuple(getattr(state, "selected_config_ids", ()) or ())
        if not selected_config_ids:
            QMessageBox.warning(self, "导入音频分析", "请先启用一个配置")
            return

        try:
            configs = [
                self.services.config_service.load_config(config_id)
                for config_id in selected_config_ids
            ]
        except Exception:
            QMessageBox.warning(self, "导入音频分析", "配置加载失败，无法导入音频分析")
            return

        default_config_id = (
            selected_config_ids[0] if len(selected_config_ids) == 1 else None
        )
        dialog = ImportAudioAnalysisDialog(
            configs=configs,
            default_config_id=default_config_id,
            parent=self,
        )
        result = dialog.exec()
        if result not in (QDialog.DialogCode.Accepted, int(QDialog.DialogCode.Accepted)):
            return

        request = dialog.current_request()
        if request is None:
            return

        analyze_imported_audio = getattr(factory_mode, "analyze_imported_audio", None)
        if not callable(analyze_imported_audio) or not analyze_imported_audio(request):
            QMessageBox.warning(
                self,
                "导入音频分析",
                "导入音频分析失败，请查看结果区域或日志",
            )

    @property
    def manual_fault_buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._manual_fault_buttons)

    def set_manual_judgment_target(
        self,
        target: ManualJudgmentTarget | None,
    ) -> None:
        if target is not None and type(target) is not ManualJudgmentTarget:
            raise TypeError(
                "target must be an exact ManualJudgmentTarget or None"
            )
        if target == self._manual_judgment_target:
            return
        self._manual_judgment_target = target
        self._selected_manual_fault_type = None
        self._manual_intent_key = None
        self.manual_fault_button_group.setExclusive(False)
        for button in self._manual_fault_buttons:
            button.setChecked(False)
        self.manual_fault_button_group.setExclusive(True)
        self._render_manual_fault_buttons()
        self._sync_manual_judgment_controls()

    def handle_manual_judgment_result(self, result: OperationResult) -> None:
        if type(result) is not OperationResult:
            raise TypeError("result must be an exact OperationResult")
        target = self._manual_judgment_target
        key = self._manual_intent_key
        if (
            result.status is OperationStatus.COMPLETED
            and target is not None
            and result.run_id == target.run_id
        ):
            self.clear_manual_judgment_selection()
            return
        if key is not None and result.run_id == key[0]:
            self._manual_intent_key = None
            self._sync_manual_judgment_controls()
        if result.status is OperationStatus.NOT_FOUND:
            self.services.bus.status.emit(
                "人工判定失败：所选运行记录不存在，可能已被删除"
            )
        elif result.status is not OperationStatus.COMPLETED:
            detail = result.reason.strip() or result.status.name
            self.services.bus.status.emit(f"人工判定保存失败：{detail}")

    def clear_manual_judgment_selection(self) -> None:
        self._selected_manual_fault_type = None
        self._manual_intent_key = None
        self.manual_fault_button_group.setExclusive(False)
        for button in self._manual_fault_buttons:
            button.setChecked(False)
        self.manual_fault_button_group.setExclusive(True)
        self._sync_manual_judgment_controls()

    def shutdown(self) -> None:
        self._configured_fault_types = ()
        self.set_manual_judgment_target(None)
        self._render_manual_fault_buttons()
        self._sync_manual_judgment_controls()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def _load_configured_fault_types(self) -> tuple[str, ...]:
        selected = tuple(
            value
            for value in getattr(
                self._factory_state,
                "selected_config_ids",
                (),
            )
            if type(value) is int and value > 0
        )
        if not selected:
            return ()
        load = getattr(
            getattr(self.services, "config_service", None),
            "load_config",
            None,
        )
        if not callable(load):
            return ()
        try:
            config = load(selected[0])
        except Exception:
            return ()
        raw = config.get("fault_types", ()) if isinstance(config, dict) else ()
        if not isinstance(raw, (list, tuple)):
            return ()
        normalized = []
        for value in raw:
            if type(value) is not str:
                continue
            value = value.strip()
            if value and value not in normalized:
                normalized.append(value)
        return tuple(normalized)

    def _render_manual_fault_buttons(self) -> None:
        target = self._manual_judgment_target
        fault_types = (
            target.fault_types
            if target is not None
            else self._configured_fault_types
        )
        render_key = (
            target.run_id if target is not None else None,
            fault_types,
        )
        if render_key == self._rendered_manual_button_key:
            return
        self._rendered_manual_button_key = render_key

        self.manual_fault_button_layout.removeWidget(self.btn_pass)
        self.manual_fault_button_layout.removeWidget(self.btn_fail)
        self.btn_pass.hide()
        self.btn_fail.hide()
        self.manual_fault_button_group.setExclusive(False)
        for button in self._manual_fault_buttons:
            button.setEnabled(False)
            button.clicked.disconnect()
            button.hide()
            button.setChecked(False)
            self.manual_fault_button_group.removeButton(button)
            self.manual_fault_button_layout.removeWidget(button)
            button.deleteLater()
        self.manual_fault_button_group.setExclusive(True)
        self._manual_fault_buttons.clear()

        for column in range(3):
            self.manual_fault_button_layout.setColumnStretch(column, 1)

        self.manual_fault_button_layout.addWidget(self.btn_pass, 0, 0)
        self.btn_pass.show()
        if not fault_types:
            self.manual_fault_button_layout.addWidget(self.btn_fail, 0, 1)
            self.btn_fail.show()
            self.manual_fault_button_layout.setColumnStretch(2, 0)
            self._stabilize_manual_widget_height()
            return

        for index, fault_type in enumerate(fault_types):
            button = QPushButton(fault_type)
            button.setObjectName("Fail")
            button.setCheckable(True)
            button.setMinimumHeight(36)
            button.pressed.connect(
                lambda source=button: setattr(
                    source,
                    "_manual_press_key",
                    (
                        self._manual_judgment_target.run_id,
                        self._manual_judgment_target.revision,
                    )
                    if self._manual_judgment_target is not None
                    else None,
                )
            )
            button.clicked.connect(
                lambda _checked=False, value=fault_type, source=button: (
                    self._select_manual_fault_type(
                        value,
                        getattr(source, "_manual_press_key", None),
                    )
                )
            )
            self.manual_fault_button_group.addButton(button)
            slot = index + 1
            self.manual_fault_button_layout.addWidget(
                button,
                slot // 3,
                slot % 3,
            )
            self._manual_fault_buttons.append(button)
        self._stabilize_manual_widget_height()

    def _stabilize_manual_widget_height(self) -> None:
        self.manual_judgment_widget.setMinimumHeight(0)
        layout = self.manual_judgment_widget.layout()
        if layout is not None:
            layout.activate()
        self.manual_judgment_widget.setMinimumHeight(
            self.manual_judgment_widget.sizeHint().height()
        )
        self.manual_judgment_widget.updateGeometry()

    def _select_manual_fault_type(
        self,
        fault_type: str,
        target_key: tuple[str, int] | None,
    ) -> None:
        target = self._manual_judgment_target
        if (
            target is None
            or target_key != (target.run_id, target.revision)
            or not target.manual_judgment_allowed
            or fault_type not in target.fault_types
        ):
            return
        self._selected_manual_fault_type = fault_type
        self._sync_manual_judgment_controls()
        self._emit_manual_judgment_intent("NG")

    def _emit_manual_judgment_intent(self, label: str) -> None:
        target = self._manual_judgment_target
        if target is None or not target.manual_judgment_allowed:
            return
        if label == "OK":
            fault_type = None
        elif label == "NG":
            if target.fault_types:
                fault_type = self._selected_manual_fault_type
                if fault_type not in target.fault_types:
                    return
            else:
                fault_type = None
        else:
            return
        self._manual_intent_key = (target.run_id, target.revision)
        self.manual_judgment_intent.emit(label, fault_type)

    def _sync_manual_judgment_controls(self) -> None:
        target = self._manual_judgment_target
        allowed = target is not None and target.manual_judgment_allowed
        for button in self._manual_fault_buttons:
            button.setEnabled(allowed)
        self.btn_pass.setEnabled(allowed)
        self.btn_fail.setEnabled(
            allowed and target is not None and not target.fault_types
        )
        self.manual_judgment_widget.setEnabled(allowed)
        self.manual_judgment_widget.setVisible(allowed)

    def _sync_start_state(self) -> None:
        production_reason = self._production_unavailable_reason()
        self.lbl_production_unavailable_reason.setText(
            production_reason or ""
        )
        self.lbl_production_unavailable_reason.setVisible(
            production_reason is not None
        )
        self.btn_start.setEnabled(not self._start_click_disabled())
        self.btn_stop.setEnabled(self._factory_state.run_phase in self._STOP_ENABLED_PHASES)
        self.btn_import_audio.setEnabled(not self._import_audio_disabled())

    def _start_disabled_reason(self) -> str | None:
        production_reason = self._production_unavailable_reason()
        if production_reason is not None:
            return production_reason
        if not self._start_click_disabled():
            return None
        return self._factory_state.start_ready_reason

    def _start_click_disabled(self) -> bool:
        return (
            self._production_unavailable_reason() is not None
            or self._factory_state.run_phase in self._BUSY_RUN_PHASES
            or self._factory_state.start_ready_reason in self._BUSY_START_READY_REASONS
        )

    def _import_audio_disabled(self) -> bool:
        return (
            self._production_unavailable_reason() is not None
            or self._factory_state.run_phase in self._BUSY_RUN_PHASES
            or self._factory_state.start_ready_reason in self._BUSY_START_READY_REASONS
        )

    def _production_unavailable_reason(self) -> str | None:
        reason = getattr(
            self.services,
            "production_unavailable_reason",
            None,
        )
        if type(reason) is not str:
            return None
        normalized = reason.strip()
        return normalized or None

    def _current_idle_blocking_reason(self) -> str | None:
        if self._start_click_disabled():
            return None
        if self._factory_state.start_ready:
            return None
        return self._factory_state.start_ready_reason

    def _sync_blocked_start_dialog(self) -> None:
        self._sync_pending_blocked_start_dialog()
        if self._blocked_start_dialog is None:
            return
        if self._blocked_start_dialog_source != "blocked_start":
            return
        reason = self._current_idle_blocking_reason()
        if not reason:
            self._close_blocked_start_dialog()
            return
        self._update_blocked_start_dialog(reason)

    def _show_or_update_blocked_start_dialog(self, reason: str, *, source: str) -> None:
        if source == "blocked_start" and self._blocked_start_dialog is None:
            self._schedule_blocked_start_dialog(reason, source=source)
            return
        self._cancel_pending_blocked_start_dialog()
        self._open_or_update_blocked_start_dialog(reason, source=source)

    def _open_or_update_blocked_start_dialog(self, reason: str, *, source: str) -> None:
        self._blocked_start_dialog_source = source
        if self._blocked_start_dialog is None:
            dialog = self._create_blocked_start_dialog()
            self._configure_blocked_start_dialog(dialog)
            self._blocked_start_dialog = dialog
            self._connect_blocked_start_dialog(dialog)
            self._update_blocked_start_dialog(reason)
            open_dialog = getattr(dialog, "open", None)
            if callable(open_dialog):
                open_dialog()
            return
        self._update_blocked_start_dialog(reason)

    def _schedule_blocked_start_dialog(self, reason: str, *, source: str) -> None:
        self._pending_blocked_start_reason = reason
        self._pending_blocked_start_source = source
        self._blocked_start_dialog_timer.start(self._BLOCKED_START_DIALOG_DELAY_MS)

    def _sync_pending_blocked_start_dialog(self) -> None:
        if self._pending_blocked_start_source != "blocked_start":
            return
        reason = self._current_idle_blocking_reason()
        if not reason:
            self._cancel_pending_blocked_start_dialog()
            return
        self._pending_blocked_start_reason = reason

    def _cancel_pending_blocked_start_dialog(self) -> None:
        self._blocked_start_dialog_timer.stop()
        self._pending_blocked_start_reason = None
        self._pending_blocked_start_source = None

    def _open_pending_blocked_start_dialog(self) -> None:
        reason = self._pending_blocked_start_reason
        source = self._pending_blocked_start_source
        self._pending_blocked_start_reason = None
        self._pending_blocked_start_source = None
        if not isinstance(reason, str) or not reason.strip():
            return
        self._open_or_update_blocked_start_dialog(reason, source=source or "blocked_start")

    def _create_blocked_start_dialog(self):
        return QMessageBox(self)

    def _configure_blocked_start_dialog(self, dialog) -> None:
        set_window_title = getattr(dialog, "setWindowTitle", None)
        if callable(set_window_title):
            set_window_title("自动开始已阻止")
        set_icon = getattr(dialog, "setIcon", None)
        if callable(set_icon):
            set_icon(QMessageBox.Icon.Warning)
        set_standard_buttons = getattr(dialog, "setStandardButtons", None)
        if callable(set_standard_buttons):
            set_standard_buttons(QMessageBox.StandardButton.Ok)

    def _connect_blocked_start_dialog(self, dialog) -> None:
        finished_signal = getattr(dialog, "finished", None)
        connect = getattr(finished_signal, "connect", None)
        if callable(connect):
            connect(self._on_blocked_start_dialog_finished)

    def _update_blocked_start_dialog(self, reason: str) -> None:
        dialog = self._blocked_start_dialog
        if dialog is None:
            return
        set_text = getattr(dialog, "setText", None)
        if callable(set_text):
            set_text(reason)
        raise_dialog = getattr(dialog, "raise_", None)
        if callable(raise_dialog):
            raise_dialog()
        activate_window = getattr(dialog, "activateWindow", None)
        if callable(activate_window):
            activate_window()

    def _close_blocked_start_dialog(self) -> None:
        self._cancel_pending_blocked_start_dialog()
        dialog = self._blocked_start_dialog
        self._blocked_start_dialog = None
        self._blocked_start_dialog_source = None
        if dialog is None:
            return
        close_dialog = getattr(dialog, "close", None)
        if callable(close_dialog):
            close_dialog()

    def _on_blocked_start_dialog_finished(self, *_args) -> None:
        self._blocked_start_dialog = None
        self._blocked_start_dialog_source = None

