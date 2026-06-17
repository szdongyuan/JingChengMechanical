from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services_container import FactoryControllerState
from ui.popups.import_audio_analysis_dialog import ImportAudioAnalysisDialog


class ControlModule(QWidget):
    module_id = "control"
    _BUSY_RUN_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _STOP_ENABLED_PHASES = frozenset({"running", "analyzing"})
    _REVIEW_CLEANUP_BUSY_REASON = "当前测试运行中"
    _PENDING_START_REASON = "启动请求处理中"
    _BUSY_START_READY_REASONS = frozenset(
        {_REVIEW_CLEANUP_BUSY_REASON, _PENDING_START_REASON}
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
        self.btn_stop = QPushButton("□ 停止测试")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setMinimumHeight(36)
        row1.addWidget(self.btn_start)
        row1.addWidget(self.btn_stop)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_pass = QPushButton("√ 手工合格")
        self.btn_pass.setObjectName("Success")
        self.btn_pass.setMinimumHeight(36)
        self.btn_fail = QPushButton("× 手工不合格")
        self.btn_fail.setObjectName("Fail")
        self.btn_fail.setMinimumHeight(36)
        row2.addWidget(self.btn_pass)
        row2.addWidget(self.btn_fail)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn_import_audio = QPushButton("导入音频分析")
        self.btn_import_audio.setMinimumHeight(36)
        row3.addWidget(self.btn_import_audio)
        lay.addLayout(row3)

        self.services.bus.workorder_changed.connect(self._on_workorder)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_start_blocked"):
            self.services.bus.factory_start_blocked.connect(self._on_factory_start_blocked)
        if hasattr(self.services.bus, "factory_runtime_aborted"):
            self.services.bus.factory_runtime_aborted.connect(self._on_factory_runtime_aborted)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_pass.clicked.connect(lambda: self._apply_manual_judgment("OK"))
        self.btn_fail.clicked.connect(lambda: self._apply_manual_judgment("NG"))
        self.btn_import_audio.clicked.connect(self._import_audio)
        self._sync_start_state()

    def _on_workorder(self, wo: dict):
        self._workorder = wo or {}
        self._sync_start_state()

    def _on_factory_state_changed(self, state):
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
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

    def _on_factory_runtime_aborted(self, payload) -> None:
        error_message = getattr(payload, "error_message", None)
        if not isinstance(error_message, str):
            return
        error_message = error_message.strip()
        if not error_message:
            return
        self._show_or_update_blocked_start_dialog(
            f"启动失败：{error_message}",
            source="runtime_abort",
        )

    def _start(self):
        if not self.btn_start.isEnabled():
            return
        factory_mode = getattr(self.services, "factory_mode", None)
        start_manual = getattr(factory_mode, "start_manual", None)
        if callable(start_manual):
            start_manual(self._workorder)

    def _stop(self):
        factory_mode = getattr(self.services, "factory_mode", None)
        stop_active_run = getattr(factory_mode, "stop_active_run", None)
        if callable(stop_active_run):
            stop_active_run()

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

    def _apply_manual_judgment(self, label: str) -> None:
        normalized = str(label or "").strip().upper()
        if normalized == "OK":
            log_message = "Manual judgment: PASS"
        elif normalized == "NG":
            log_message = "Manual judgment: FAIL"
        else:
            self._show_manual_judgment_warning("无效判定结果")
            return

        factory_mode = getattr(self.services, "factory_mode", None)
        apply_manual_judgment = getattr(factory_mode, "apply_manual_judgment", None)
        if not callable(apply_manual_judgment):
            self._show_manual_judgment_warning("当前测试不支持人工判定")
            return

        result = apply_manual_judgment(normalized)
        if not getattr(result, "success", False):
            message = str(getattr(result, "message", "") or "保存人工判定失败")
            self._show_manual_judgment_warning(message)
            return

        self.services.bus.log.emit(log_message)

    def _show_manual_judgment_warning(self, message: str) -> None:
        QMessageBox.warning(self, "提示", f"人工判定失败：{message}")

    def _sync_start_state(self) -> None:
        self.btn_start.setEnabled(not self._start_click_disabled())
        self.btn_stop.setEnabled(self._factory_state.run_phase in self._STOP_ENABLED_PHASES)
        self.btn_import_audio.setEnabled(not self._import_audio_disabled())

    def _start_disabled_reason(self) -> str | None:
        if not self._start_click_disabled():
            return None
        return self._factory_state.start_ready_reason

    def _start_click_disabled(self) -> bool:
        return (
            self._factory_state.run_phase in self._BUSY_RUN_PHASES
            or self._factory_state.start_ready_reason in self._BUSY_START_READY_REASONS
        )

    def _import_audio_disabled(self) -> bool:
        return (
            self._factory_state.run_phase in self._BUSY_RUN_PHASES
            or self._factory_state.start_ready_reason in self._BUSY_START_READY_REASONS
        )

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

