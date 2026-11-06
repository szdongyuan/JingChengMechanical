from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QListWidget, QPushButton, QSpinBox

from app.service.result_dashboard_layout import DEFAULT_RESULT_DASHBOARD_LAYOUT
from ui.widgets.result_dashboard_layout_editor import ResultDashboardLayoutEditor


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _show_editor(editor: ResultDashboardLayoutEditor, qapp) -> None:
    editor.resize(720, 420)
    editor.show()
    qapp.processEvents()


def _cell_center(editor: ResultDashboardLayoutEditor, row: int, column: int) -> QPoint:
    preview = editor.grid_preview()
    layout = editor.layout_data()
    cell_width = (preview.width() - 20) / layout["columns"]
    cell_height = (preview.height() - 20) / layout["rows"]
    return QPoint(
        round(10 + (column + 0.5) * cell_width),
        round(10 + (row + 0.5) * cell_height),
    )


def _panel_by_id(editor: ResultDashboardLayoutEditor, panel_id: str) -> dict:
    return next(panel for panel in editor.layout_data()["panels"] if panel["id"] == panel_id)


def test_editor_blocks_removing_row_occupied_by_span(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 2,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 2,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    assert editor.remove_last_row() is False
    assert editor.layout_data()["rows"] == 2


def test_editor_blocks_removing_any_occupied_row(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 3,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 1,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    assert editor.remove_row(1) is False
    assert editor.layout_data()["rows"] == 3


def test_editor_removes_empty_middle_row_and_shifts_lower_panels(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 3,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 2,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    assert editor.remove_row(1) is True
    assert editor.layout_data()["rows"] == 2
    spectrum = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == "spectrum"
    )
    assert spectrum["row"] == 1


def test_editor_stages_add_panel_and_channel_binding(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    panel_id = editor.add_panel("spectrum", row=0, column=1, channel="selected_1")
    panel = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == panel_id
    )
    assert panel["type"] == "spectrum"
    assert panel["channel"] == "selected_1"


def test_editor_catalog_and_default_have_no_result_summary(qapp):
    editor = ResultDashboardLayoutEditor()
    catalog_types = [
        editor.panel_catalog_combo.itemData(index)
        for index in range(editor.panel_catalog_combo.count())
    ]

    assert "result_summary" not in catalog_types
    assert "result_summary" not in {
        panel["type"] for panel in editor.layout_data()["panels"]
    }


def test_editor_display_fallback_replaces_legacy_layout(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 1,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "summary",
                    "type": "result_summary",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "none",
                },
                {
                    "id": "legacy-spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )

    assert editor.layout_data() == DEFAULT_RESULT_DASHBOARD_LAYOUT


def test_editor_allows_duplicate_non_summary_panel_types(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 3,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum_a",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    panel_id = editor.add_panel("spectrum", row=0, column=2, channel="selected_2")
    assert panel_id is not None
    assert [panel["type"] for panel in editor.layout_data()["panels"]].count("spectrum") == 2


def test_editor_blocks_add_panel_to_occupied_explicit_cell(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )

    assert editor.add_panel("spectrum", row=0, column=0) is None
    assert [panel["id"] for panel in editor.layout_data()["panels"]] == ["stft"]


def test_editor_panel_catalog_add_button_adds_panel_to_list(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    _show_editor(editor, qapp)
    catalog = editor.findChild(QComboBox, "result_dashboard_panel_catalog_combo")
    add_button = editor.findChild(QPushButton, "result_dashboard_add_panel_button")
    panel_list = editor.findChild(QListWidget, "result_dashboard_panel_list")

    catalog.setCurrentIndex(catalog.findData("spectrum"))
    QTest.mouseClick(
        editor.grid_preview(),
        Qt.MouseButton.LeftButton,
        pos=_cell_center(editor, 0, 1),
    )
    QTest.mouseClick(add_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    assert panel_list.count() == 2
    assert any(panel["type"] == "spectrum" for panel in editor.layout_data()["panels"])


def test_editor_grid_click_selects_empty_cell_for_visible_add_button(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 2,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    _show_editor(editor, qapp)
    catalog = editor.findChild(QComboBox, "result_dashboard_panel_catalog_combo")
    add_button = editor.findChild(QPushButton, "result_dashboard_add_panel_button")

    catalog.setCurrentIndex(catalog.findData("spectrum"))
    QTest.mouseClick(
        editor.grid_preview(),
        Qt.MouseButton.LeftButton,
        pos=_cell_center(editor, 1, 1),
    )
    QTest.mouseClick(add_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    spectrum = next(
        panel for panel in editor.layout_data()["panels"] if panel["type"] == "spectrum"
    )
    assert (spectrum["row"], spectrum["column"]) == (1, 1)


def test_editor_visible_add_button_uses_empty_cell_after_grid_expands(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 1,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    _show_editor(editor, qapp)
    columns = editor.findChild(QComboBox, "result_dashboard_column_count_combo")
    add_row = editor.findChild(QPushButton, "result_dashboard_add_row_button")
    catalog = editor.findChild(QComboBox, "result_dashboard_panel_catalog_combo")
    add_panel = editor.findChild(QPushButton, "result_dashboard_add_panel_button")

    columns.setCurrentText("2")
    QTest.mouseClick(add_row, Qt.MouseButton.LeftButton)
    catalog.setCurrentIndex(catalog.findData("spectrum"))
    QTest.mouseClick(add_panel, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    spectrum = next(
        panel for panel in editor.layout_data()["panels"] if panel["type"] == "spectrum"
    )
    assert (spectrum["row"], spectrum["column"]) != (0, 0)
    assert spectrum["row"] < editor.layout_data()["rows"]
    assert spectrum["column"] < editor.layout_data()["columns"]


def test_editor_remove_panel_button_removes_selected_optional_panel(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    panel_list = editor.findChild(QListWidget, "result_dashboard_panel_list")
    remove_button = editor.findChild(QPushButton, "result_dashboard_remove_panel_button")

    editor.select_panel("spectrum")
    remove_button.click()
    qapp.processEvents()

    assert panel_list.count() == 1
    assert [panel["id"] for panel in editor.layout_data()["panels"]] == ["stft"]


def test_editor_updates_existing_panel_channel_binding(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    assert editor.set_panel_channel("spectrum", "selected_2") is True
    spectrum = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == "spectrum"
    )
    assert spectrum["channel"] == "selected_2"


def test_editor_channel_combo_updates_existing_panel_binding(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    editor.select_panel("spectrum")
    combo = editor.findChild(QComboBox, "result_dashboard_channel_combo")
    combo.setCurrentIndex(combo.findData("selected_2"))
    qapp.processEvents()

    spectrum = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == "spectrum"
    )
    assert spectrum["channel"] == "selected_2"


def test_editor_moves_panel_and_blocks_overlap(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 2,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    assert editor.move_panel("spectrum", row=1, column=0) is True
    spectrum = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == "spectrum"
    )
    assert (spectrum["row"], spectrum["column"]) == (1, 0)
    assert editor.move_panel("spectrum", row=0, column=0) is False


def test_editor_blocks_column_reduction_when_panel_would_be_out_of_bounds(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 3,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 2,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    assert editor.set_columns(2) is False
    assert editor.layout_data()["columns"] == 3


def test_editor_column_combo_changes_column_count(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 1,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    combo = editor.findChild(QComboBox, "result_dashboard_column_count_combo")
    combo.setCurrentText("3")
    qapp.processEvents()

    assert editor.layout_data()["columns"] == 3


def test_editor_add_and_remove_row_buttons_update_rows(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 1,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    add_button = editor.findChild(QPushButton, "result_dashboard_add_row_button")
    remove_button = editor.findChild(QPushButton, "result_dashboard_remove_row_button")
    remove_row = editor.findChild(QSpinBox, "result_dashboard_remove_row_spin")

    assert remove_row.minimum() == 1
    assert remove_row.value() == 1

    add_button.click()
    qapp.processEvents()
    assert editor.layout_data()["rows"] == 2
    assert remove_row.maximum() == 2

    remove_row.setValue(2)
    remove_button.click()
    qapp.processEvents()
    assert editor.layout_data()["rows"] == 1
    assert remove_row.maximum() == 1


def test_editor_blocks_span_change_that_would_overlap(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    assert editor.set_panel_span("stft", row_span=1, column_span=2) is False
    stft = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == "stft"
    )
    assert (stft["row_span"], stft["column_span"]) == (1, 1)


def test_editor_blocks_span_change_that_would_exceed_grid(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
            ],
        }
    )
    assert editor.set_panel_span("stft", row_span=2, column_span=1) is False
    stft = editor.layout_data()["panels"][0]
    assert (stft["row_span"], stft["column_span"]) == (1, 1)


def test_editor_span_spin_boxes_restore_previous_values_after_invalid_span(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 1,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    editor.select_panel("stft")
    column_span = editor.findChild(QSpinBox, "result_dashboard_column_span_spin")
    column_span.setValue(2)
    qapp.processEvents()

    assert column_span.value() == 1
    stft = next(
        panel for panel in editor.layout_data()["panels"] if panel["id"] == "stft"
    )
    assert stft["column_span"] == 1


def test_every_default_panel_is_removable(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(None)

    default_ids = [panel["id"] for panel in editor.layout_data()["panels"]]
    for panel_id in default_ids:
        assert editor.remove_panel(panel_id) is True

    assert editor.layout_data()["panels"] == []


def test_editor_grid_mouse_drag_moves_panel(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 2,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    _show_editor(editor, qapp)

    preview = editor.grid_preview()
    QTest.mousePress(
        preview,
        Qt.MouseButton.LeftButton,
        pos=_cell_center(editor, 0, 1),
    )
    QTest.mouseMove(preview, pos=_cell_center(editor, 1, 0))
    QTest.mouseRelease(
        preview,
        Qt.MouseButton.LeftButton,
        pos=_cell_center(editor, 1, 0),
    )
    qapp.processEvents()

    spectrum = _panel_by_id(editor, "spectrum")
    assert (spectrum["row"], spectrum["column"]) == (1, 0)


def test_editor_grid_mouse_drag_rejects_overlap(qapp):
    editor = ResultDashboardLayoutEditor()
    editor.set_layout(
        {
            "version": 2,
            "columns": 2,
            "rows": 2,
            "panels": [
                {
                    "id": "stft",
                    "type": "stft",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "primary_acoustic",
                },
                {
                    "id": "spectrum",
                    "type": "spectrum",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "selected_1",
                },
            ],
        }
    )
    _show_editor(editor, qapp)

    preview = editor.grid_preview()
    QTest.mousePress(
        preview,
        Qt.MouseButton.LeftButton,
        pos=_cell_center(editor, 0, 1),
    )
    QTest.mouseMove(preview, pos=_cell_center(editor, 0, 0))
    QTest.mouseRelease(
        preview,
        Qt.MouseButton.LeftButton,
        pos=_cell_center(editor, 0, 0),
    )
    qapp.processEvents()

    spectrum = _panel_by_id(editor, "spectrum")
    assert (spectrum["row"], spectrum["column"]) == (0, 1)
