from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from app.service.config_package_service import (
    ConfigPackageExportOptions,
    ConfigPackageImportDecision,
    ConfigPackagePreview,
)

DY_PACKAGE_FILTER = "DY 配置包 (*.dy)"

_ACTION_LABELS = {
    "import": "导入",
    "overwrite": "覆盖",
    "use_existing_config": "使用旧配置",
    "use_existing_toolchain": "使用旧工具链",
    "clear_binding": "清空 AI 绑定",
    "cancel": "取消",
}


class ConfigPackageExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出配置包")
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        intro = QLabel("请选择要随配置一起导出的内容：", self)
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.chk_include_golden_samples = QCheckBox("包含黄金样本", self)
        self.chk_include_ai_toolchain = QCheckBox("包含 AI 工具链", self)
        self.chk_include_ai_package = QCheckBox("包含 AI 包", self)

        root.addWidget(self.chk_include_golden_samples)
        root.addWidget(self.chk_include_ai_toolchain)
        root.addWidget(self.chk_include_ai_package)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        root.addWidget(buttons)

        self.chk_include_ai_toolchain.toggled.connect(
            self._refresh_ai_package_enabled
        )
        self._refresh_ai_package_enabled()

    def options(self) -> ConfigPackageExportOptions:
        include_ai_toolchain = self.chk_include_ai_toolchain.isChecked()
        return ConfigPackageExportOptions(
            include_golden_samples=self.chk_include_golden_samples.isChecked(),
            include_ai_toolchain=include_ai_toolchain,
            include_ai_package=(
                include_ai_toolchain and self.chk_include_ai_package.isChecked()
            ),
        )

    def _refresh_ai_package_enabled(self) -> None:
        enabled = self.chk_include_ai_toolchain.isChecked()
        self.chk_include_ai_package.setEnabled(enabled)
        if not enabled:
            self.chk_include_ai_package.setChecked(False)


def choose_export_options(parent=None) -> ConfigPackageExportOptions | None:
    dialog = ConfigPackageExportDialog(parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.options()


def choose_dy_export_path(parent=None, *, default_name: str = "config") -> str | None:
    suggested_name = _ensure_dy_suffix(_sanitize_default_name(default_name))
    path, _selected_filter = QFileDialog.getSaveFileName(
        parent,
        "导出配置包",
        suggested_name,
        DY_PACKAGE_FILTER,
    )
    if not path:
        return None
    return _ensure_dy_suffix(path)


def choose_dy_import_path(parent=None) -> str | None:
    path, _selected_filter = QFileDialog.getOpenFileName(
        parent,
        "导入配置包",
        "",
        DY_PACKAGE_FILTER,
    )
    return path or None


def format_import_preview_text(preview: ConfigPackagePreview) -> str:
    contents = dict(preview.contents or {})
    lines = [
        f"源配置名称：{preview.config_name}",
        f"包含黄金样本：{_yes_no(contents.get('golden_samples'))}",
        f"包含 AI 工具链：{_yes_no(contents.get('ai_toolchain'))}",
        f"包含 AI 包：{_yes_no(contents.get('ai_package'))}",
    ]
    if preview.ai_toolchain_id:
        lines.append(f"AI 工具链 ID：{preview.ai_toolchain_id}")
    if preview.warnings:
        lines.append("")
        lines.append("提示：")
        lines.extend(str(warning) for warning in preview.warnings)
    return "\n".join(lines)


def choose_import_decision(
    parent,
    preview: ConfigPackagePreview,
) -> ConfigPackageImportDecision | None:
    preview_text = format_import_preview_text(preview)
    config_action = preview.default_config_action
    ai_action = preview.default_ai_action or "create"
    shown_dialog = False

    if preview.config_conflict:
        config_action = _ask_action_choice(
            parent,
            title="导入配置包",
            text=f"{preview_text}\n\n检测到同名配置，请选择处理方式。",
            actions=preview.config_action_options,
            default_action=preview.default_config_action,
        )
        shown_dialog = True
        if config_action in (None, "cancel"):
            return None
        if config_action == "use_existing_config":
            return ConfigPackageImportDecision(
                config_action="use_existing_config",
                ai_action="create",
            )

    if preview.ai_conflict and preview.ai_action_options:
        ai_text = f"{preview_text}\n\n检测到 AI 工具链冲突，请选择处理方式。"
        if "overwrite" in preview.ai_action_options:
            ai_text += (
                "\n覆盖 AI 工具链可能影响绑定到同一工具链 ID 的其他配置。"
            )
        ai_action = _ask_action_choice(
            parent,
            title="导入配置包",
            text=ai_text,
            actions=preview.ai_action_options,
            default_action=preview.default_ai_action,
        )
        shown_dialog = True
        if ai_action in (None, "cancel"):
            return None

    if not shown_dialog:
        action = _ask_action_choice(
            parent,
            title="导入配置包",
            text=f"{preview_text}\n\n确认导入此配置包？",
            actions=("import", "cancel"),
            default_action="import",
        )
        if action != "import":
            return None

    return ConfigPackageImportDecision(
        config_action=config_action,
        ai_action=ai_action,
    )


def _ask_action_choice(
    parent,
    *,
    title: str,
    text: str,
    actions: tuple[str, ...],
    default_action: str | None,
) -> str | None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)

    buttons = {}
    for action in actions:
        label = _ACTION_LABELS[action]
        role = (
            QMessageBox.ButtonRole.RejectRole
            if action == "cancel"
            else QMessageBox.ButtonRole.AcceptRole
        )
        buttons[action] = box.addButton(label, role)

    if default_action in buttons:
        box.setDefaultButton(buttons[default_action])

    box.exec()
    clicked = box.clickedButton()
    for action, button in buttons.items():
        if clicked is button:
            return action
    return None


def _ensure_dy_suffix(path: str) -> str:
    normalized = str(path)
    if normalized.lower().endswith(".dy"):
        return normalized
    return f"{normalized}.dy"


def _sanitize_default_name(name: str) -> str:
    cleaned = str(name or "").strip() or "config"
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", cleaned)
    return cleaned


def _yes_no(value) -> str:
    return "是" if bool(value) else "否"
