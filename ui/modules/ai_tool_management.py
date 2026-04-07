from __future__ import annotations

from PyQt6.QtCore import QRegularExpression, Qt
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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

        self.spin_timeout = _LargeIntegerEditor()
        self.spin_timeout.setRange(0, 2_147_483_647)
        self.spin_timeout.setSingleStep(1000)
        self.spin_timeout.setSuffix(" ms")
        self.spin_timeout.setValue(30000)
        form_lay.addRow("默认超时", self.spin_timeout)

        self.chk_enabled = QCheckBox("启用")
        form_lay.addRow("状态", self.chk_enabled)

        self.txt_description = QPlainTextEdit()
        self.txt_description.setMaximumHeight(100)
        form_lay.addRow("描述", self.txt_description)

        self.runtime_helper = QLabel(
            "运行时固定: protocol_version = 1, execution_mode = oneshot_process"
        )
        self.runtime_helper.setObjectName("Muted")
        self.runtime_helper.setWordWrap(True)
        form_lay.addRow("运行时", self.runtime_helper)

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
        if current is None:
            self._update_action_state()
            return
        toolchain_id = current.data(Qt.ItemDataRole.UserRole)
        row = self._toolchain_rows.get(toolchain_id)
        if row is None:
            self._update_action_state()
            return
        self._load_toolchain(row)

    def _load_toolchain(self, row: dict):
        self._current_toolchain_id = row["toolchain_id"]
        self._is_new_draft = False
        self._editor_snapshot = dict(row)
        self._set_editor_payload(row)
        self.empty_label.setVisible(False)
        self.detail.setVisible(True)
        self._update_action_state()

    def _set_editor_payload(self, row: dict):
        self.ed_toolchain_id.setText(row.get("toolchain_id", ""))
        self.ed_display_name.setText(row.get("display_name", ""))
        self.ed_version.setText(row.get("version", ""))
        self.ed_entrypoint.setPlainText(row.get("entrypoint", ""))
        self.spin_timeout.setValue(int(row.get("default_timeout_ms") or 0))
        self.chk_enabled.setChecked(bool(row.get("enabled", True)))
        self.txt_description.setPlainText(row.get("description", ""))

    def _clear_editor(self):
        self.ed_toolchain_id.clear()
        self.ed_display_name.clear()
        self.ed_version.clear()
        self.ed_entrypoint.clear()
        self.spin_timeout.setValue(30000)
        self.chk_enabled.setChecked(True)
        self.txt_description.clear()

    def _on_new(self):
        self._current_toolchain_id = None
        self._editor_snapshot = None
        self._is_new_draft = True
        self.toolchain_list.blockSignals(True)
        self.toolchain_list.setCurrentRow(-1)
        self.toolchain_list.clearSelection()
        self.toolchain_list.blockSignals(False)
        self._clear_editor()
        self.empty_label.setVisible(False)
        self.detail.setVisible(True)
        self._update_action_state()

    def _on_delete(self):
        return

    def _collect_payload(self) -> dict:
        return {
            "toolchain_id": self.ed_toolchain_id.text().strip(),
            "display_name": self.ed_display_name.text().strip(),
            "version": self.ed_version.text().strip(),
            "entrypoint": self.ed_entrypoint.toPlainText().strip(),
            "default_timeout_ms": self.spin_timeout.value(),
            "enabled": self.chk_enabled.isChecked(),
            "description": self.txt_description.toPlainText().strip(),
        }

    def _validate_payload(self, payload: dict):
        if (
            not payload["toolchain_id"]
            or not payload["display_name"]
            or not payload["entrypoint"]
        ):
            raise ValueError("工具链标识、显示名称和入口命令不能为空")
        if payload["default_timeout_ms"] <= 0:
            raise ValueError("默认超时必须大于 0")
        duplicate = payload["toolchain_id"] in self._toolchain_rows
        if duplicate and payload["toolchain_id"] != self._current_toolchain_id:
            raise ValueError("工具链标识已存在")

    def _on_save(self):
        payload = self._collect_payload()
        try:
            self._validate_payload(payload)
            saved_toolchain_id = self.services.ai_service.save_toolchain(
                self._current_toolchain_id,
                payload,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return

        self._current_toolchain_id = saved_toolchain_id
        self._is_new_draft = False
        self._refresh_list(selected_toolchain_id=saved_toolchain_id)
        ai_toolchains_changed = getattr(self.services.bus, "ai_toolchains_changed", None)
        if ai_toolchains_changed is not None:
            ai_toolchains_changed.emit()

    def _update_action_state(self):
        # Delete remains deferred until Task 4 adds the actual mediation flow.
        self.btn_delete.setEnabled(False)

    def confirm_leave(self) -> bool:
        return True
