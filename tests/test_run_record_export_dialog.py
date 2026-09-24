from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog

from app.service.run_record_export import RunDataExportOptions


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_dialog_has_exact_text_defaults_and_requires_one_option(qapp):
    from ui.popups.run_record_export_dialog import RunRecordExportDialog

    dialog = RunRecordExportDialog(selected_count=12)

    assert dialog.windowTitle() == "导出设置"
    assert dialog.selected_count_label.text() == "已选择 12 条记录"
    assert dialog.chk_include_excel.text() == "Excel 汇总"
    assert dialog.chk_include_audio.text() == "音频文件"
    assert dialog.chk_include_database.text() == "数据库副本"
    assert dialog.chk_include_current_csv.text() == "电流数据 CSV"
    assert dialog.chk_include_excel.isChecked() is True
    assert dialog.chk_include_audio.isChecked() is True
    assert dialog.chk_include_database.isChecked() is True
    assert dialog.chk_include_current_csv.isChecked() is False
    assert dialog.accept_button.text() == "确定导出"
    assert dialog.accept_button.isEnabled() is True

    for checkbox in (
        dialog.chk_include_excel,
        dialog.chk_include_audio,
        dialog.chk_include_database,
        dialog.chk_include_current_csv,
    ):
        checkbox.setChecked(False)

    assert dialog.accept_button.isEnabled() is False

    dialog.chk_include_current_csv.setChecked(True)
    assert dialog.accept_button.isEnabled() is True
    dialog.close()


def test_dialog_returns_immutable_options_and_each_instance_has_fresh_defaults(
    qapp,
):
    from ui.popups.run_record_export_dialog import RunRecordExportDialog

    first = RunRecordExportDialog(selected_count=1)
    first.chk_include_excel.setChecked(False)
    first.chk_include_audio.setChecked(False)
    first.chk_include_database.setChecked(False)
    first.chk_include_current_csv.setChecked(True)

    assert first.options() == RunDataExportOptions(
        include_excel=False,
        include_audio=False,
        include_database=False,
        include_current_csv=True,
    )
    with pytest.raises(Exception):
        first.options().include_excel = True

    second = RunRecordExportDialog(selected_count=2)
    assert second.options() == RunDataExportOptions.defaults()
    first.close()
    second.close()


def test_choose_options_returns_none_on_cancel_and_options_on_accept(
    qapp,
    monkeypatch,
):
    from ui.popups import run_record_export_dialog as dialog_module

    monkeypatch.setattr(
        dialog_module.RunRecordExportDialog,
        "exec",
        lambda _self: QDialog.DialogCode.Rejected,
    )
    assert (
        dialog_module.choose_run_export_options(None, selected_count=3) is None
    )

    monkeypatch.setattr(
        dialog_module.RunRecordExportDialog,
        "exec",
        lambda _self: QDialog.DialogCode.Accepted,
    )
    assert dialog_module.choose_run_export_options(
        None,
        selected_count=3,
    ) == RunDataExportOptions.defaults()


@pytest.mark.parametrize(
    ("output_kind", "chosen", "expected", "expected_filter"),
    (
        ("xlsx", "C:/exports/report", "C:/exports/report.xlsx", "Excel 文件 (*.xlsx)"),
        ("xlsx", "C:/exports/report.XLSX", "C:/exports/report.XLSX", "Excel 文件 (*.xlsx)"),
        ("zip", "C:/exports/report", "C:/exports/report.zip", "ZIP 文件 (*.zip)"),
        ("zip", "C:/exports/report.ZIP", "C:/exports/report.ZIP", "ZIP 文件 (*.zip)"),
    ),
)
def test_path_chooser_uses_kind_filter_and_normalizes_expected_suffix(
    qapp,
    monkeypatch,
    output_kind,
    chosen,
    expected,
    expected_filter,
):
    from ui.popups import run_record_export_dialog as dialog_module

    calls = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args: (calls.append(args), (chosen, expected_filter))[1],
    )

    result = dialog_module.choose_run_export_path(
        None,
        output_kind=output_kind,
        initial_dir="C:/exports",
    )

    assert result == expected
    assert calls[0][1] == "导出运行记录"
    assert calls[0][2].replace("\\", "/") == (
        f"C:/exports/运行记录导出.{output_kind}"
    )
    assert calls[0][3] == expected_filter


def test_path_chooser_returns_none_when_canceled(qapp, monkeypatch):
    from ui.popups import run_record_export_dialog as dialog_module

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args: ("", ""),
    )

    assert (
        dialog_module.choose_run_export_path(
            None,
            output_kind="zip",
            initial_dir="C:/exports",
        )
        is None
    )
