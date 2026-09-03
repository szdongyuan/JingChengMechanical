from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.factory_config_list import FactoryConfigListWidget


class FactoryConfigSelectionDialog(QDialog):
    _LOCKED_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _PENDING_START_REASON = "启动请求处理中"
    _SUCCESS_STATUSES = frozenset(
        {"applied", "restart_required", "unchanged"}
    )
    _LOCK_REASONS = {
        "running": "当前测试运行中",
        "analyzing": "当前测试分析中",
        "reanalyzing": "当前正在重新分析",
    }

    def __init__(self, services, parent: QWidget | None = None):
        super().__init__(parent)
        self.services = services
        self._baseline_selected_ids = tuple(
            services.factory_mode.state.selected_config_ids
        )
        self._draft_selected_ids = self._baseline_selected_ids
        self._pending_authoritative_ids: tuple[int, ...] | None = None
        self._pending_authoritative_warn = False
        self._last_loaded_rows: list[dict] | None = None
        self._load_failed = False
        self._phase_locked = False
        self._notice_kind = ""

        self.setWindowTitle("配置选择")
        self.setModal(True)
        self.resize(520, 440)

        self.instructions_label = QLabel(
            "勾选生产使用的配置，拖拽可调整优先顺序，第一项为首选配置。",
            self,
        )
        self.instructions_label.setWordWrap(True)
        self.config_selector = FactoryConfigListWidget(self)
        self.empty_label = QLabel("配置列表为空，请先新建配置。", self)
        self.lock_notice_label = QLabel(self)
        self.lock_notice_label.setWordWrap(True)
        self.notice_label = QLabel(self)
        self.notice_label.setWordWrap(True)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.confirm_button = self.button_box.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.confirm_button.setText("确定")
        self.button_box.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("取消")

        layout = QVBoxLayout(self)
        layout.addWidget(self.instructions_label)
        layout.addWidget(self.config_selector, 1)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.lock_notice_label)
        layout.addWidget(self.notice_label)
        layout.addWidget(self.button_box)

        self.config_selector.enabled_order_changed.connect(
            self._on_draft_changed
        )
        self.button_box.accepted.connect(self._confirm)
        self.button_box.rejected.connect(self.reject)
        self._connect_bus_signal(
            "factory_state_changed",
            self._on_factory_state_changed,
        )
        self._connect_bus_signal("config_changed", self._on_config_changed)

        self.prepare_for_open()

    def prepare_for_open(self) -> None:
        state = self.services.factory_mode.state
        selected = tuple(state.selected_config_ids)
        self._set_notice("")
        self._pending_authoritative_ids = selected
        self._pending_authoritative_warn = False
        if self._reload_rows(selected):
            self._baseline_selected_ids = selected
            self._pending_authoritative_ids = None
        self._apply_lock_state(state)

    def _confirm(self) -> None:
        selected = self.config_selector.enabled_config_ids()
        if not selected:
            self._set_notice("请至少选择一个配置。", kind="validation")
            return
        if self.config_selector.is_locked():
            self._set_notice(
                self._lock_reason(self.services.factory_mode.state),
                kind="lock",
            )
            return

        try:
            result = self.services.factory_mode.confirm_selection(selected)
        except Exception:
            self._set_notice("配置保存失败", kind="result")
            return

        message = str(getattr(result, "message", "") or "")
        status = getattr(result, "status", None)
        if status in self._SUCCESS_STATUSES:
            self._set_notice(message, kind="result")
            self.accept()
            return

        self._set_notice(message or "配置保存失败", kind="result")

    def _on_factory_state_changed(self, state) -> None:
        selected = tuple(getattr(state, "selected_config_ids", ()))
        authority_changed = selected != self._baseline_selected_ids
        retry_pending_authority = self._pending_authoritative_ids == selected
        if (
            self._pending_authoritative_ids is not None
            and not authority_changed
            and not retry_pending_authority
        ):
            self._pending_authoritative_ids = None
            self._pending_authoritative_warn = False

        rebuild_from_authority = authority_changed or retry_pending_authority
        warn_for_authoritative_change = authority_changed or (
            retry_pending_authority and self._pending_authoritative_warn
        )
        if rebuild_from_authority:
            self._pending_authoritative_ids = selected
            self._pending_authoritative_warn = warn_for_authoritative_change
            loaded = self._reload_rows(selected)
            if loaded:
                self._baseline_selected_ids = selected
                self._pending_authoritative_ids = None
                self._pending_authoritative_warn = False
        elif self._load_failed:
            loaded = self._reload_rows(self._draft_selected_ids)
        else:
            loaded = True

        self._apply_lock_state(state)
        if rebuild_from_authority and loaded and warn_for_authoritative_change:
            self._set_notice(
                "配置选择已更新，请重新确认。",
                kind="conflict",
            )

    def _on_config_changed(self) -> None:
        pending_authoritative = self._pending_authoritative_ids
        target = (
            pending_authoritative
            if pending_authoritative is not None
            else self._draft_selected_ids
        )
        warn_for_authoritative_change = self._pending_authoritative_warn
        if not self._reload_rows(target):
            return
        if pending_authoritative is not None:
            self._baseline_selected_ids = pending_authoritative
            self._pending_authoritative_ids = None
            self._pending_authoritative_warn = False
            if warn_for_authoritative_change:
                self._set_notice(
                    "配置选择已更新，请重新确认。",
                    kind="conflict",
                )
        if self.config_selector.enabled_config_ids() != target:
            self._set_notice(
                "部分配置已失效，请重新确认。",
                kind="prune",
            )

    def _reload_rows(self, enabled_config_ids: tuple[int, ...]) -> bool:
        list_configs = getattr(
            getattr(self.services, "config_service", None),
            "list_configs",
            None,
        )
        if not callable(list_configs):
            self._set_load_failure()
            return False
        try:
            rows = list(list_configs())
        except Exception:
            self._set_load_failure()
            return False

        self._load_failed = False
        self._last_loaded_rows = [dict(row) for row in rows]
        self.config_selector.set_rows(
            rows=rows,
            enabled_config_ids=enabled_config_ids,
        )
        self._draft_selected_ids = self.config_selector.enabled_config_ids()
        if self._notice_kind == "load":
            self._set_notice("")
        self._update_empty_and_confirm_state()
        return True

    def _apply_lock_state(self, state) -> None:
        self._phase_locked = self._is_locked(state)
        self.lock_notice_label.setText(
            self._lock_reason(state) if self._phase_locked else ""
        )
        self.lock_notice_label.setVisible(self._phase_locked)
        self._update_empty_and_confirm_state()

    def _update_empty_and_confirm_state(self) -> None:
        is_empty = self.config_selector.count() == 0
        interaction_locked = self._phase_locked or self._load_failed
        self.config_selector.set_locked(interaction_locked)
        self.empty_label.setVisible(is_empty and not self._load_failed)
        self.confirm_button.setEnabled(
            not is_empty and not interaction_locked
        )

    def _set_load_failure(self) -> None:
        self._load_failed = True
        self._set_notice("配置加载失败", kind="load")
        self._update_empty_and_confirm_state()

    def _on_draft_changed(self, selected_config_ids: tuple[int, ...]) -> None:
        self._draft_selected_ids = tuple(selected_config_ids)

    def _is_locked(self, state) -> bool:
        return (
            self._persistence_lock_active()
            or getattr(state, "run_phase", None) in self._LOCKED_PHASES
            or getattr(state, "start_ready_reason", None)
            == self._PENDING_START_REASON
        )

    def _lock_reason(self, state) -> str:
        if self._persistence_lock_active():
            return str(
                getattr(state, "start_ready_reason", None)
                or getattr(state, "selection_ready_reason", None)
                or "当前无法修改配置选择"
            )
        if (
            getattr(state, "start_ready_reason", None)
            == self._PENDING_START_REASON
        ):
            return self._PENDING_START_REASON
        return self._LOCK_REASONS.get(
            getattr(state, "run_phase", None),
            "当前无法修改配置选择",
        )

    def _persistence_lock_active(self) -> bool:
        return bool(
            getattr(
                getattr(self.services, "factory_mode", None),
                "selection_persistence_indeterminate",
                False,
            )
        )

    def _set_notice(self, message: str, *, kind: str = "") -> None:
        self.notice_label.setText(message)
        self.notice_label.setVisible(bool(message))
        self._notice_kind = kind if message else ""

    def _connect_bus_signal(self, signal_name: str, callback) -> None:
        signal = getattr(getattr(self.services, "bus", None), signal_name, None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(callback)
