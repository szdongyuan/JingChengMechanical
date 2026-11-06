from __future__ import annotations

import os
from typing import Literal

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QVBoxLayout,
)

from app.service.run_record_export import RunDataExportOptions


class RunRecordExportDialog(QDialog):
    def __init__(self, *, selected_count: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出设置")
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.selected_count_label = QLabel(
            f"已选择 {selected_count} 条记录",
            self,
        )
        root.addWidget(self.selected_count_label)

        defaults = RunDataExportOptions.defaults()
        self.chk_include_excel = QCheckBox("Excel 汇总", self)
        self.chk_include_audio = QCheckBox("音频文件", self)
        self.chk_include_database = QCheckBox("数据库副本", self)
        self.chk_include_current_csv = QCheckBox("电流数据 CSV", self)
        self.chk_include_excel.setChecked(defaults.include_excel)
        self.chk_include_audio.setChecked(defaults.include_audio)
        self.chk_include_database.setChecked(defaults.include_database)
        self.chk_include_current_csv.setChecked(defaults.include_current_csv)

        self._checkboxes = (
            self.chk_include_excel,
            self.chk_include_audio,
            self.chk_include_database,
            self.chk_include_current_csv,
        )
        for checkbox in self._checkboxes:
            root.addWidget(checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        self.accept_button = buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.accept_button.setText("确定导出")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for checkbox in self._checkboxes:
            checkbox.toggled.connect(self._refresh_accept_enabled)
        self._refresh_accept_enabled()

    def options(self) -> RunDataExportOptions:
        return RunDataExportOptions(
            include_excel=self.chk_include_excel.isChecked(),
            include_audio=self.chk_include_audio.isChecked(),
            include_database=self.chk_include_database.isChecked(),
            include_current_csv=self.chk_include_current_csv.isChecked(),
        )

    def _refresh_accept_enabled(self) -> None:
        self.accept_button.setEnabled(
            any(checkbox.isChecked() for checkbox in self._checkboxes)
        )


def choose_run_export_options(
    parent,
    *,
    selected_count: int,
) -> RunDataExportOptions | None:
    dialog = RunRecordExportDialog(
        parent=parent,
        selected_count=selected_count,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.options()


def choose_run_export_path(
    parent,
    *,
    output_kind: Literal["xlsx", "zip"],
    initial_dir: str,
) -> str | None:
    if output_kind == "xlsx":
        suffix = ".xlsx"
        save_filter = "Excel 文件 (*.xlsx)"
    elif output_kind == "zip":
        suffix = ".zip"
        save_filter = "ZIP 文件 (*.zip)"
    else:
        raise ValueError("output kind must be xlsx or zip")

    suggested_path = os.path.join(
        initial_dir,
        f"运行记录导出{suffix}",
    )
    path, _selected_filter = QFileDialog.getSaveFileName(
        parent,
        "导出运行记录",
        suggested_path,
        save_filter,
    )
    if not path:
        return None
    if str(path).lower().endswith(suffix):
        return str(path)
    return f"{path}{suffix}"
