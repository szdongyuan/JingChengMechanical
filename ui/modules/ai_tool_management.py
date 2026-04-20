from __future__ import annotations

import os

from PyQt6.QtCore import QRegularExpression, Qt
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_DEFAULT_EXECUTION_MODE = "resident_process"


class _LargeIntegerEditor(QLineEdit):
    """QLineEdit-based integer editor that supports values beyond QSpinBox limits."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._suffix = ""
        self._single_step = 1
        self._minimum = 0
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setValidator(
            QRegularExpressionValidator(
                QRegularExpression(r"(?:0|[1-9]\d*)?"),
                self,
            )
        )

    def setRange(self, minimum: int, _maximum: int):
        self._minimum = minimum

    def setSingleStep(self, step: int):
        self._single_step = step

    def setSuffix(self, suffix: str):
        self._suffix = suffix
        if suffix:
            self.setPlaceholderText(suffix.strip())

    def setValue(self, value: int):
        self.setText(str(int(value)))

    def value(self) -> int:
        text = self.text().strip()
        if not text:
            return max(self._minimum, 0)
        return int(text)


class AiToolManagementModule(QWidget):
    module_id = "ai_tool_management"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._toolchain_rows: dict[str, dict] = {}
        self._current_toolchain_id: str | None = None
        self._editor_snapshot: dict | None = None
        self._is_new_draft = False
        self._suspend_editor_change_tracking = False
        self._suppress_selection_change = False
        self._init_ui()
        self._connect_signals()
        self._refresh_list()

    def _init_ui(self):
        self.setStyleSheet("QWidget { font-size: 14px; }")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        left = QWidget()
        left.setFixedWidth(240)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)

        button_row = QWidget()
        button_lay = QHBoxLayout(button_row)
        button_lay.setContentsMargins(0, 0, 0, 0)
        button_lay.setSpacing(6)
        self.btn_new = QPushButton("新建")
        self.btn_new.setObjectName("Primary")
        self.btn_delete = QPushButton("删除")
        self.btn_delete.setObjectName("Danger")
        button_lay.addWidget(self.btn_new)
        button_lay.addWidget(self.btn_delete)
        left_lay.addWidget(button_row)

        self.toolchain_list = QListWidget()
        self.toolchain_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.toolchain_list.setStyleSheet(
            "QListWidget { background: #f8fafc; border: 1px solid #e5e7eb; "
            "border-radius: 8px; padding: 4px; }"
            "QListWidget::item { padding: 8px; border-radius: 6px; "
            "border: 1px solid transparent; margin: 2px 0; }"
            "QListWidget::item:selected { background: #e0edff; "
            "border: 2px solid #3b82f6; color: #1e40af; font-weight: 600; }"
            "QListWidget::item:hover:!selected { background: #f1f5f9; }"
        )
        left_lay.addWidget(self.toolchain_list, 1)
        root.addWidget(left)

        right = QWidget()
        self.right_lay = QVBoxLayout(right)
        self.right_lay.setContentsMargins(0, 0, 0, 0)
        self.right_lay.setSpacing(10)

        self.empty_label = QLabel("暂无 AI 工具链，请点击「新建」创建")
        self.empty_label.setObjectName("Muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right_lay.addWidget(self.empty_label)

        self.detail = QWidget()
        detail_lay = QVBoxLayout(self.detail)
        detail_lay.setContentsMargins(0, 0, 0, 0)
        detail_lay.setSpacing(10)

        form_host = QWidget()
        form_lay = QFormLayout(form_host)
        form_lay.setContentsMargins(0, 0, 0, 0)
        form_lay.setHorizontalSpacing(10)
        form_lay.setVerticalSpacing(8)

        self.ed_toolchain_id = QLineEdit()
        form_lay.addRow("工具链标识", self.ed_toolchain_id)

        self.ed_display_name = QLineEdit()
        form_lay.addRow("显示名称", self.ed_display_name)

        self.ed_version = QLineEdit()
        form_lay.addRow("版本", self.ed_version)

        self.ed_entrypoint = QPlainTextEdit()
        self.ed_entrypoint.setMaximumHeight(80)
        form_lay.addRow("入口命令", self.ed_entrypoint)

        package_dir_row = QWidget()
        package_dir_lay = QHBoxLayout(package_dir_row)
        package_dir_lay.setContentsMargins(0, 0, 0, 0)
        package_dir_lay.setSpacing(8)
        self.ed_package_dir = QLineEdit()
        self.btn_browse_package_dir = QPushButton("浏览")
        package_dir_lay.addWidget(self.ed_package_dir, 1)
        package_dir_lay.addWidget(self.btn_browse_package_dir)
        form_lay.addRow("AI 包目录", package_dir_row)

        self.spin_timeout = _LargeIntegerEditor()
        self.spin_timeout.setRange(0, 2_147_483_647)
        self.spin_timeout.setSingleStep(1000)
        self.spin_timeout.setSuffix(" ms")
        self.spin_timeout.setValue(30000)
        form_lay.addRow("默认超时", self.spin_timeout)

        self.chk_enabled = QCheckBox("启用")
        form_lay.addRow("状态", self.chk_enabled)

        self.combo_execution_mode = QComboBox()
        self.combo_execution_mode.addItem("常驻进程（推荐）", "resident_process")
        self.combo_execution_mode.addItem("单次进程", "oneshot_process")
        form_lay.addRow("执行模式", self.combo_execution_mode)

        self.txt_description = QPlainTextEdit()
        self.txt_description.setMaximumHeight(100)
        form_lay.addRow("描述", self.txt_description)

        self.runtime_helper = QLabel(
            "protocol_version = 1（固定）；推荐使用 resident_process 以复用预加载模型；"
            "oneshot_process 适用于仍使用基于文件 CLI 的工具链。"
        )
        self.runtime_helper.setObjectName("Muted")
        self.runtime_helper.setWordWrap(True)
        form_lay.addRow("运行时说明", self.runtime_helper)

        detail_lay.addWidget(form_host)

        save_row = QWidget()
        save_lay = QHBoxLayout(save_row)
        save_lay.setContentsMargins(0, 0, 0, 0)
        save_lay.addStretch(1)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("Primary")
        save_lay.addWidget(self.btn_save)
        detail_lay.addWidget(save_row)

        self.right_lay.addWidget(self.detail, 1)
        root.addWidget(right, 1)

    def _connect_signals(self):
        self.btn_new.clicked.connect(self._on_new)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_save.clicked.connect(self._on_save)
        self.toolchain_list.currentItemChanged.connect(self._on_item_changed)
        self.ed_toolchain_id.textChanged.connect(self._on_editor_changed)
        self.ed_display_name.textChanged.connect(self._on_editor_changed)
        self.ed_version.textChanged.connect(self._on_editor_changed)
        self.ed_entrypoint.textChanged.connect(self._on_editor_changed)
        self.ed_package_dir.textChanged.connect(self._on_editor_changed)
        self.spin_timeout.textChanged.connect(self._on_editor_changed)
        self.chk_enabled.toggled.connect(self._on_editor_changed)
        self.combo_execution_mode.currentIndexChanged.connect(self._on_editor_changed)
        self.txt_description.textChanged.connect(self._on_editor_changed)
        self.btn_browse_package_dir.clicked.connect(self._browse_package_dir)

    def _refresh_list(self, selected_toolchain_id: str | None = None):
        rows = sorted(
            self.services.ai_service.list_toolchains(),
            key=lambda row: row["toolchain_id"],
        )
        self._toolchain_rows = {
            row["toolchain_id"]: dict(row)
            for row in rows
        }

        self.toolchain_list.blockSignals(True)
        self.toolchain_list.clear()
        for row in rows:
            item = QListWidgetItem(self._format_list_text(row))
            item.setData(Qt.ItemDataRole.UserRole, row["toolchain_id"])
            self.toolchain_list.addItem(item)
        self.toolchain_list.blockSignals(False)

        has_items = bool(rows)
        should_show_detail = has_items or self._is_new_draft
        self.empty_label.setVisible(not should_show_detail)
        self.detail.setVisible(should_show_detail)

        if selected_toolchain_id is not None:
            self._select_toolchain(selected_toolchain_id)
        elif has_items and not self._is_new_draft:
            self.toolchain_list.setCurrentRow(0)
        else:
            self._update_action_state()

    @staticmethod
    def _empty_draft_payload() -> dict:
        return {
            "toolchain_id": "",
            "display_name": "",
            "version": "",
            "entrypoint": "",
            "package_dir": "",
            "default_timeout_ms": 30000,
            "enabled": True,
            "execution_mode": _DEFAULT_EXECUTION_MODE,
            "description": "",
        }

    @classmethod
    def _normalize_editor_payload(cls, row: dict | None) -> dict:
        payload = cls._empty_draft_payload()
        if row is None:
            return payload
        payload.update(
            {
                "toolchain_id": row.get("toolchain_id", ""),
                "display_name": row.get("display_name", ""),
                "version": row.get("version", ""),
                "entrypoint": row.get("entrypoint", ""),
                "package_dir": row.get("package_dir", ""),
                "default_timeout_ms": int(row.get("default_timeout_ms") or 0),
                "enabled": bool(row.get("enabled", True)),
                "execution_mode": row.get("execution_mode", _DEFAULT_EXECUTION_MODE),
                "description": row.get("description", ""),
            }
        )
        return payload

    @staticmethod
    def _format_list_text(row: dict) -> str:
        version = row.get("version") or "-"
        text = f'{row["display_name"]} ({version}) - {row["toolchain_id"]}'
        if not row.get("enabled", True):
            text = f"{text} [禁用]"
        return text

    def _select_toolchain(self, toolchain_id: str):
        for index in range(self.toolchain_list.count()):
            item = self.toolchain_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == toolchain_id:
                self.toolchain_list.setCurrentRow(index)
                return

    def _on_item_changed(self, current: QListWidgetItem | None, _previous):
        if self._suppress_selection_change:
            return
        if current is None:
            self._update_action_state()
            return
        toolchain_id = current.data(Qt.ItemDataRole.UserRole)
        if toolchain_id == self._current_toolchain_id and not self._is_new_draft:
            self._update_action_state()
            return
        if not self._resolve_pending_editor_action("switch"):
            self._restore_selection()
            return
        row = self._toolchain_rows.get(toolchain_id)
        if row is None:
            self._restore_selection()
            self._update_action_state()
            return
        self._set_selection_by_toolchain_id(toolchain_id)
        self._load_toolchain(row)

    def _load_toolchain(self, row: dict):
        self._current_toolchain_id = row["toolchain_id"]
        self._is_new_draft = False
        self._editor_snapshot = self._normalize_editor_payload(row)
        self._set_editor_payload(self._editor_snapshot)
        self.empty_label.setVisible(False)
        self.detail.setVisible(True)
        self._update_action_state()

    def _set_editor_payload(self, row: dict):
        self._suspend_editor_change_tracking = True
        try:
            self.ed_toolchain_id.setText(row.get("toolchain_id", ""))
            self.ed_display_name.setText(row.get("display_name", ""))
            self.ed_version.setText(row.get("version", ""))
            self.ed_entrypoint.setPlainText(row.get("entrypoint", ""))
            self.ed_package_dir.setText(row.get("package_dir", ""))
            self.spin_timeout.setValue(int(row.get("default_timeout_ms") or 0))
            self.chk_enabled.setChecked(bool(row.get("enabled", True)))
            self._set_execution_mode(
                row.get("execution_mode", _DEFAULT_EXECUTION_MODE)
            )
            self.txt_description.setPlainText(row.get("description", ""))
        finally:
            self._suspend_editor_change_tracking = False

    def _browse_package_dir(self):
        start_dir = self.ed_package_dir.text().strip() or os.getcwd()
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "选择 AI 包目录",
            start_dir,
        )
        if selected_dir:
            self.ed_package_dir.setText(selected_dir)

    def _set_execution_mode(self, execution_mode: str):
        index = self.combo_execution_mode.findData(execution_mode)
        if index < 0:
            index = self.combo_execution_mode.findData(_DEFAULT_EXECUTION_MODE)
        self.combo_execution_mode.setCurrentIndex(index)

    def _clear_editor(self):
        self._set_editor_payload(self._empty_draft_payload())

    def _on_new(self):
        if not self._resolve_pending_editor_action("new"):
            return
        self._current_toolchain_id = None
        self._editor_snapshot = self._empty_draft_payload()
        self._is_new_draft = True
        self._clear_selection()
        self._clear_editor()
        self.empty_label.setVisible(False)
        self.detail.setVisible(True)
        self._update_action_state()

    def _on_delete(self):
        toolchain_id = self._current_toolchain_id
        if toolchain_id is None:
            return
        if not self._resolve_pending_editor_action("delete"):
            return
        toolchain_id = self._current_toolchain_id
        if toolchain_id is None:
            return
        row = self._toolchain_rows.get(toolchain_id)
        if row is None:
            return
        try:
            bindings = self.services.ai_service.list_toolchain_bindings(toolchain_id)
            should_delete = self._confirm_delete(row, bindings)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        if not should_delete:
            return
        try:
            self.services.ai_service.delete_toolchain(toolchain_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return

        self._current_toolchain_id = None
        self._editor_snapshot = None
        self._is_new_draft = False
        self._refresh_list()
        self._emit_ai_toolchains_changed()

    def _collect_payload(self) -> dict:
        return {
            "toolchain_id": self.ed_toolchain_id.text(),
            "display_name": self.ed_display_name.text(),
            "version": self.ed_version.text(),
            "entrypoint": self.ed_entrypoint.toPlainText(),
            "package_dir": self.ed_package_dir.text(),
            "default_timeout_ms": self.spin_timeout.value(),
            "enabled": self.chk_enabled.isChecked(),
            "execution_mode": self.combo_execution_mode.currentData()
            or _DEFAULT_EXECUTION_MODE,
            "description": self.txt_description.toPlainText(),
        }

    def _collect_save_payload(self) -> dict:
        payload = self._collect_payload()
        return {
            "toolchain_id": payload["toolchain_id"].strip(),
            "display_name": payload["display_name"].strip(),
            "version": payload["version"].strip(),
            "entrypoint": payload["entrypoint"].strip(),
            "package_dir": payload["package_dir"].strip(),
            "default_timeout_ms": payload["default_timeout_ms"],
            "enabled": payload["enabled"],
            "execution_mode": payload["execution_mode"],
            "description": payload["description"].strip(),
        }

    def _validate_payload(self, payload: dict):
        if (
            not payload["toolchain_id"]
            or not payload["display_name"]
            or not payload["entrypoint"]
        ):
            raise ValueError("工具链标识、显示名称和入口命令不能为空")
        if not payload["package_dir"]:
            raise ValueError("AI 包目录不能为空")
        if not os.path.isdir(payload["package_dir"]):
            raise ValueError("AI 包目录必须是已存在的目录")
        if payload["default_timeout_ms"] <= 0:
            raise ValueError("默认超时必须大于 0")
        duplicate = payload["toolchain_id"] in self._toolchain_rows
        if duplicate and payload["toolchain_id"] != self._current_toolchain_id:
            raise ValueError("工具链标识已存在")

    def _on_save(self):
        self._save_current_editor()

    def _save_current_editor(self) -> bool:
        payload = self._collect_save_payload()
        try:
            self._validate_payload(payload)
            saved_toolchain_id = self.services.ai_service.save_toolchain(
                self._current_toolchain_id,
                payload,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return False
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return False

        self._current_toolchain_id = saved_toolchain_id
        self._is_new_draft = False
        self._refresh_list(selected_toolchain_id=saved_toolchain_id)
        persisted_row = self._toolchain_rows.get(saved_toolchain_id)
        if persisted_row is not None:
            self._load_toolchain(persisted_row)
        else:
            normalized_payload = self._normalize_editor_payload(payload)
            self._editor_snapshot = normalized_payload
            self._set_editor_payload(normalized_payload)
        self._emit_ai_toolchains_changed()
        return True

    def _emit_ai_toolchains_changed(self):
        ai_toolchains_changed = getattr(self.services.bus, "ai_toolchains_changed", None)
        if ai_toolchains_changed is not None:
            ai_toolchains_changed.emit()

    def _on_editor_changed(self, *_args):
        if self._suspend_editor_change_tracking:
            return
        self._update_action_state()

    def _has_active_editor(self) -> bool:
        return self._is_new_draft or self._current_toolchain_id is not None

    def _is_dirty(self) -> bool:
        if not self._has_active_editor():
            return False
        snapshot = self._editor_snapshot or self._empty_draft_payload()
        return self._collect_payload() != snapshot

    def _clear_selection(self):
        self._suppress_selection_change = True
        try:
            self.toolchain_list.setCurrentRow(-1)
            self.toolchain_list.clearSelection()
        finally:
            self._suppress_selection_change = False

    def _restore_selection(self):
        if self._current_toolchain_id is None:
            self._clear_selection()
            self._update_action_state()
            return
        self._set_selection_by_toolchain_id(self._current_toolchain_id)
        self._update_action_state()

    def _set_selection_by_toolchain_id(self, toolchain_id: str):
        self._suppress_selection_change = True
        try:
            for index in range(self.toolchain_list.count()):
                item = self.toolchain_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == toolchain_id:
                    self.toolchain_list.setCurrentRow(index)
                    return
            self.toolchain_list.setCurrentRow(-1)
            self.toolchain_list.clearSelection()
        finally:
            self._suppress_selection_change = False

    def _restore_snapshot(self):
        if not self._has_active_editor():
            return
        snapshot = self._editor_snapshot or self._empty_draft_payload()
        self._set_editor_payload(snapshot)

    def _prompt_unsaved_changes(self, action: str) -> str:
        action_label = {
            "switch": "切换工具链",
            "new": "新建工具链",
            "delete": "删除当前工具链",
            "leave": "离开当前页面",
        }.get(action, "继续当前操作")
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("未保存的更改")
        dialog.setText(f"当前编辑内容尚未保存。是否先保存后再{action_label}？")
        save_button = dialog.addButton("保存并继续", QMessageBox.ButtonRole.AcceptRole)
        discard_button = dialog.addButton("放弃更改", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(save_button)
        dialog.setEscapeButton(cancel_button)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    def _resolve_pending_editor_action(self, action: str) -> bool:
        if not self._is_dirty():
            return True

        decision = self._prompt_unsaved_changes(action)
        if decision == "cancel":
            return False
        if decision == "discard":
            self._restore_snapshot()
            return True
        if decision == "save":
            return self._save_current_editor()
        return False

    def _build_delete_confirmation_text(self, row: dict, bindings: list[dict]) -> str:
        version = row.get("version") or "-"
        identity = f'{row.get("display_name") or row["toolchain_id"]} ({version}) / {row["toolchain_id"]}'
        lines = [f"确定要删除 AI 工具链「{identity}」吗？"]
        if bindings:
            lines.append(f"受影响配置（{len(bindings)} 个）:")
            for binding in bindings:
                config_name = binding.get("config_name") or str(binding.get("config_id"))
                config_id = binding.get("config_id")
                if config_id is None:
                    lines.append(f"- {config_name}")
                else:
                    lines.append(f"- {config_name} (ID: {config_id})")
        else:
            lines.append("受影响配置：0 个")
        lines.append("删除后这些配置将解除绑定，需要重新选择 AI 工具链。")
        return "\n".join(lines)

    def _confirm_delete(self, row: dict, bindings: list[dict]) -> bool:
        reply = QMessageBox.question(
            self,
            "确认删除",
            self._build_delete_confirmation_text(row, bindings),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _update_action_state(self):
        can_delete = self.toolchain_list.currentItem() is not None and not self._is_new_draft
        self.btn_delete.setEnabled(can_delete)
        self.btn_save.setEnabled(self._has_active_editor())

    def confirm_leave(self) -> bool:
        return self._resolve_pending_editor_action("leave")
