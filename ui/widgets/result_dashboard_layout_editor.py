from __future__ import annotations

from copy import deepcopy

from PyQt6.QtCore import Qt, QSignalBlocker
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.service.result_dashboard_layout import (
    ALLOWED_CHANNELS_BY_PANEL_FAMILY,
    CHANNEL_NONE,
    CHANNEL_PRIMARY_ACOUSTIC,
    CHANNEL_SELECTED_1,
    CHANNEL_SELECTED_2,
    CHANNEL_VIBRATION,
    DEFAULT_RESULT_DASHBOARD_LAYOUT,
    LAYOUT_VERSION,
    MAX_COLUMNS,
    MIN_COLUMNS,
    MIN_ROWS,
    PANEL_FAMILIES,
    PANEL_FAMILY_ACOUSTIC,
    PANEL_FAMILY_SUMMARY,
    PANEL_FAMILY_VIBRATION,
    PANEL_RESULT_SUMMARY,
    PANEL_TITLES,
    normalize_result_dashboard_layout_for_display,
)

_CHANNEL_LABELS = {
    CHANNEL_NONE: "无",
    CHANNEL_PRIMARY_ACOUSTIC: "主声学通道",
    CHANNEL_SELECTED_1: "通道1",
    CHANNEL_SELECTED_2: "通道2",
    CHANNEL_VIBRATION: "振动通道",
}

_DEFAULT_CHANNEL_BY_FAMILY = {
    PANEL_FAMILY_SUMMARY: CHANNEL_NONE,
    PANEL_FAMILY_ACOUSTIC: CHANNEL_PRIMARY_ACOUSTIC,
    PANEL_FAMILY_VIBRATION: CHANNEL_VIBRATION,
}


class _DataComboBox(QComboBox):
    def setCurrentData(self, data) -> None:
        index = self.findData(data)
        if index >= 0:
            self.setCurrentIndex(index)


class _GridPreview(QFrame):
    def __init__(self, editor: "ResultDashboardLayoutEditor"):
        super().__init__()
        self._editor = editor
        self.setMinimumSize(360, 240)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("result_dashboard_grid_preview")
        self.setStyleSheet(
            "QFrame#result_dashboard_grid_preview { background: #f8fafc; "
            "border: 1px solid #cbd5e1; border-radius: 6px; }"
        )

    def simulate_drop_for_test(self, panel_id: str, *, row: int, column: int) -> bool:
        return self._editor.move_panel(panel_id, row=row, column=column)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        layout = self._editor.layout_data()
        columns = max(MIN_COLUMNS, int(layout.get("columns", MIN_COLUMNS)))
        rows = max(MIN_ROWS, int(layout.get("rows", MIN_ROWS)))
        width = max(1, self.width() - 20)
        height = max(1, self.height() - 20)
        cell_width = width / columns
        cell_height = height / rows

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(10, 10)
        grid_pen = QPen(QColor("#cbd5e1"))
        painter.setPen(grid_pen)
        for column in range(columns + 1):
            x = round(column * cell_width)
            painter.drawLine(x, 0, x, height)
        for row in range(rows + 1):
            y = round(row * cell_height)
            painter.drawLine(0, y, width, y)

        for panel in layout.get("panels", []):
            x = round(panel["column"] * cell_width) + 3
            y = round(panel["row"] * cell_height) + 3
            rect_width = round(panel["column_span"] * cell_width) - 6
            rect_height = round(panel["row_span"] * cell_height) - 6
            selected = panel["id"] == self._editor._selected_panel_id
            painter.setBrush(QColor("#dbeafe" if selected else "#ffffff"))
            painter.setPen(QPen(QColor("#2563eb" if selected else "#94a3b8"), 2))
            painter.drawRoundedRect(x, y, max(4, rect_width), max(4, rect_height), 5, 5)
            painter.setPen(QColor("#0f172a"))
            painter.drawText(
                x + 6,
                y + 6,
                max(4, rect_width - 12),
                max(4, rect_height - 12),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                PANEL_TITLES.get(panel["type"], panel["type"]),
            )


class ResultDashboardLayoutEditor(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = deepcopy(DEFAULT_RESULT_DASHBOARD_LAYOUT)
        self._selected_panel_id: str | None = None
        self._selected_grid_cell = (0, 0)
        self._updating_controls = False

        self._build_ui()
        self.set_layout(None)

    def set_layout(self, raw_layout) -> None:
        self._layout = normalize_result_dashboard_layout_for_display(raw_layout)
        self._selected_grid_cell = (0, 0)
        panel_ids = [panel["id"] for panel in self._layout["panels"]]
        self._selected_panel_id = "summary" if "summary" in panel_ids else panel_ids[0]
        self._refresh_controls()

    def layout_data(self) -> dict:
        return deepcopy(self._layout)

    def add_panel(
        self,
        panel_type: str,
        row: int | None = None,
        column: int | None = None,
        channel: str | None = None,
    ) -> str | None:
        if panel_type not in PANEL_TITLES:
            return None
        if panel_type == PANEL_RESULT_SUMMARY and any(
            panel["type"] == PANEL_RESULT_SUMMARY for panel in self._layout["panels"]
        ):
            return None

        explicit_target = row is not None or column is not None
        if row is None or column is None:
            selected_row, selected_column = self._selected_grid_cell
            row = selected_row if row is None else row
            column = selected_column if column is None else column

        family = PANEL_FAMILIES[panel_type]
        channel = channel or _DEFAULT_CHANNEL_BY_FAMILY[family]
        if channel not in ALLOWED_CHANNELS_BY_PANEL_FAMILY[family]:
            return None

        panel = {
            "id": self._unique_panel_id(panel_type),
            "type": panel_type,
            "row": int(row),
            "column": int(column),
            "row_span": 1,
            "column_span": 1,
            "channel": channel,
        }
        if not self._can_place(panel):
            if explicit_target:
                return None
            empty_cell = self._first_empty_cell()
            if empty_cell is None:
                return None
            panel["row"], panel["column"] = empty_cell
            if not self._can_place(panel):
                return None

        self._layout["panels"].append(panel)
        self._selected_panel_id = panel["id"]
        self._selected_grid_cell = (panel["row"], panel["column"])
        self._refresh_controls()
        return panel["id"]

    def remove_panel(self, panel_id: str) -> bool:
        panel = self._find_panel(panel_id)
        if panel is None or panel["type"] == PANEL_RESULT_SUMMARY:
            return False
        self._layout["panels"] = [
            existing for existing in self._layout["panels"] if existing["id"] != panel_id
        ]
        self._selected_panel_id = self._layout["panels"][0]["id"]
        self._refresh_controls()
        return True

    def select_panel(self, panel_id: str) -> bool:
        panel = self._find_panel(panel_id)
        if panel is None:
            return False
        self._selected_panel_id = panel_id
        self._selected_grid_cell = (panel["row"], panel["column"])
        self._refresh_controls()
        return True

    def select_grid_cell_for_test(self, row: int, column: int) -> None:
        if 0 <= row < self._layout["rows"] and 0 <= column < self._layout["columns"]:
            self._selected_grid_cell = (int(row), int(column))
            self._preview.update()

    def set_panel_channel(self, panel_id: str, channel: str) -> bool:
        panel = self._find_panel(panel_id)
        if panel is None:
            return False
        family = PANEL_FAMILIES.get(panel["type"])
        if channel not in ALLOWED_CHANNELS_BY_PANEL_FAMILY.get(family, set()):
            return False
        panel["channel"] = channel
        self._refresh_controls()
        return True

    def move_panel(self, panel_id: str, row: int, column: int) -> bool:
        panel = self._find_panel(panel_id)
        if panel is None:
            return False
        candidate = {**panel, "row": int(row), "column": int(column)}
        if not self._can_place(candidate, ignore_panel_id=panel_id):
            return False
        panel["row"] = int(row)
        panel["column"] = int(column)
        self._selected_panel_id = panel_id
        self._selected_grid_cell = (panel["row"], panel["column"])
        self._refresh_controls()
        return True

    def set_panel_span(self, panel_id: str, row_span: int, column_span: int) -> bool:
        panel = self._find_panel(panel_id)
        if panel is None:
            return False
        candidate = {
            **panel,
            "row_span": int(row_span),
            "column_span": int(column_span),
        }
        if not self._can_place(candidate, ignore_panel_id=panel_id):
            return False
        panel["row_span"] = int(row_span)
        panel["column_span"] = int(column_span)
        self._refresh_controls()
        return True

    def set_columns(self, columns: int) -> bool:
        if isinstance(columns, bool):
            return False
        columns = int(columns)
        if columns < MIN_COLUMNS or columns > MAX_COLUMNS:
            return False
        if any(panel["column"] + panel["column_span"] > columns for panel in self._layout["panels"]):
            self._refresh_controls()
            return False
        self._layout["columns"] = columns
        selected_row, selected_column = self._selected_grid_cell
        self._selected_grid_cell = (selected_row, min(selected_column, columns - 1))
        self._refresh_controls()
        return True

    def add_row(self) -> int:
        self._layout["rows"] += 1
        self._refresh_controls()
        return self._layout["rows"]

    def remove_row(self, removed_row: int) -> bool:
        if isinstance(removed_row, bool):
            return False
        removed_row = int(removed_row)
        if self._layout["rows"] <= MIN_ROWS:
            return False
        if removed_row < 0 or removed_row >= self._layout["rows"]:
            return False
        for panel in self._layout["panels"]:
            if panel["row"] <= removed_row < panel["row"] + panel["row_span"]:
                return False

        for panel in self._layout["panels"]:
            if panel["row"] > removed_row:
                panel["row"] -= 1
        self._layout["rows"] -= 1
        selected_row, selected_column = self._selected_grid_cell
        if selected_row > removed_row:
            selected_row -= 1
        self._selected_grid_cell = (min(selected_row, self._layout["rows"] - 1), selected_column)
        self._refresh_controls()
        return True

    def remove_last_row(self) -> bool:
        return self.remove_row(self._layout["rows"] - 1)

    def grid_preview(self) -> _GridPreview:
        return self._preview

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("列数"))
        self.column_count_combo = _DataComboBox()
        self.column_count_combo.setObjectName("result_dashboard_column_count_combo")
        for columns in range(MIN_COLUMNS, MAX_COLUMNS + 1):
            self.column_count_combo.addItem(str(columns), columns)
        self.column_count_combo.currentIndexChanged.connect(self._on_columns_changed)
        toolbar.addWidget(self.column_count_combo)

        self.add_row_button = QPushButton("添加行")
        self.add_row_button.setObjectName("result_dashboard_add_row_button")
        self.add_row_button.clicked.connect(lambda _checked=False: self.add_row())
        toolbar.addWidget(self.add_row_button)

        toolbar.addWidget(QLabel("删除行"))
        self.remove_row_spin = QSpinBox()
        self.remove_row_spin.setObjectName("result_dashboard_remove_row_spin")
        self.remove_row_spin.setRange(0, 0)
        toolbar.addWidget(self.remove_row_spin)

        self.remove_row_button = QPushButton("删除行")
        self.remove_row_button.setObjectName("result_dashboard_remove_row_button")
        self.remove_row_button.clicked.connect(
            lambda _checked=False: self.remove_row(self.remove_row_spin.value())
        )
        toolbar.addWidget(self.remove_row_button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        body = QHBoxLayout()
        body.setSpacing(12)
        self._preview = _GridPreview(self)
        body.addWidget(self._preview, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        self.panel_list = QListWidget()
        self.panel_list.setObjectName("result_dashboard_panel_list")
        self.panel_list.currentItemChanged.connect(self._on_panel_list_selection_changed)
        side.addWidget(self.panel_list, 1)

        catalog_row = QHBoxLayout()
        self.panel_catalog_combo = _DataComboBox()
        self.panel_catalog_combo.setObjectName("result_dashboard_panel_catalog_combo")
        for panel_type, title in PANEL_TITLES.items():
            self.panel_catalog_combo.addItem(title, panel_type)
        catalog_row.addWidget(self.panel_catalog_combo, 1)

        self.add_panel_button = QPushButton("添加")
        self.add_panel_button.setObjectName("result_dashboard_add_panel_button")
        self.add_panel_button.clicked.connect(self._on_add_panel_clicked)
        catalog_row.addWidget(self.add_panel_button)
        side.addLayout(catalog_row)

        self.remove_panel_button = QPushButton("移除")
        self.remove_panel_button.setObjectName("result_dashboard_remove_panel_button")
        self.remove_panel_button.clicked.connect(self._on_remove_panel_clicked)
        side.addWidget(self.remove_panel_button)

        property_grid = QGridLayout()
        property_grid.setHorizontalSpacing(8)
        property_grid.setVerticalSpacing(6)

        property_grid.addWidget(QLabel("通道"), 0, 0)
        self.channel_combo = _DataComboBox()
        self.channel_combo.setObjectName("result_dashboard_channel_combo")
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        property_grid.addWidget(self.channel_combo, 0, 1)

        property_grid.addWidget(QLabel("行跨度"), 1, 0)
        self.row_span_spin = QSpinBox()
        self.row_span_spin.setObjectName("result_dashboard_row_span_spin")
        self.row_span_spin.setRange(1, 1)
        self.row_span_spin.valueChanged.connect(self._on_span_changed)
        property_grid.addWidget(self.row_span_spin, 1, 1)

        property_grid.addWidget(QLabel("列跨度"), 2, 0)
        self.column_span_spin = QSpinBox()
        self.column_span_spin.setObjectName("result_dashboard_column_span_spin")
        self.column_span_spin.setRange(1, 1)
        self.column_span_spin.valueChanged.connect(self._on_span_changed)
        property_grid.addWidget(self.column_span_spin, 2, 1)
        side.addLayout(property_grid)
        body.addLayout(side, 0)
        root.addLayout(body, 1)

    def _refresh_controls(self) -> None:
        self._updating_controls = True
        try:
            self._set_combo_data(self.column_count_combo, self._layout["columns"])
            self.remove_row_spin.setMaximum(max(0, self._layout["rows"] - 1))
            self.remove_row_spin.setEnabled(self._layout["rows"] > MIN_ROWS)
            self.remove_row_button.setEnabled(self._layout["rows"] > MIN_ROWS)
            self._refresh_panel_list()
            self._refresh_property_controls()
        finally:
            self._updating_controls = False
        self._preview.update()

    def _refresh_panel_list(self) -> None:
        current_id = self._selected_panel_id
        self.panel_list.clear()
        selected_row = -1
        for row, panel in enumerate(self._layout["panels"]):
            title = PANEL_TITLES.get(panel["type"], panel["type"])
            item = QListWidgetItem(f"{title} ({panel['id']})")
            item.setData(Qt.ItemDataRole.UserRole, panel["id"])
            self.panel_list.addItem(item)
            if panel["id"] == current_id:
                selected_row = row
        if selected_row >= 0:
            self.panel_list.setCurrentRow(selected_row)

    def _refresh_property_controls(self) -> None:
        panel = self._find_panel(self._selected_panel_id)
        has_panel = panel is not None
        self.remove_panel_button.setEnabled(
            has_panel and panel["type"] != PANEL_RESULT_SUMMARY
        )
        self.channel_combo.setEnabled(has_panel)
        self.row_span_spin.setEnabled(has_panel)
        self.column_span_spin.setEnabled(has_panel)
        if not has_panel:
            self.channel_combo.clear()
            return

        self.channel_combo.clear()
        family = PANEL_FAMILIES[panel["type"]]
        for channel in self._channels_for_family(family):
            self.channel_combo.addItem(_CHANNEL_LABELS[channel], channel)
        self._set_combo_data(self.channel_combo, panel["channel"])

        self.row_span_spin.setRange(1, max(1, self._layout["rows"]))
        self.column_span_spin.setRange(1, max(1, self._layout["columns"]))
        self._set_spin_value(self.row_span_spin, panel["row_span"])
        self._set_spin_value(self.column_span_spin, panel["column_span"])

    def _on_columns_changed(self) -> None:
        if self._updating_controls:
            return
        data = self.column_count_combo.currentData()
        if data is None or not self.set_columns(int(data)):
            self._refresh_controls()

    def _on_panel_list_selection_changed(self, current, _previous) -> None:
        if self._updating_controls or current is None:
            return
        panel_id = current.data(Qt.ItemDataRole.UserRole)
        if panel_id:
            self.select_panel(panel_id)

    def _on_add_panel_clicked(self) -> None:
        panel_type = self.panel_catalog_combo.currentData()
        if panel_type:
            row, column = self._selected_grid_cell
            self.add_panel(panel_type, row=row, column=column)

    def _on_remove_panel_clicked(self) -> None:
        if self._selected_panel_id is not None:
            self.remove_panel(self._selected_panel_id)

    def _on_channel_changed(self) -> None:
        if self._updating_controls or self._selected_panel_id is None:
            return
        channel = self.channel_combo.currentData()
        if channel is None or not self.set_panel_channel(self._selected_panel_id, channel):
            self._refresh_controls()

    def _on_span_changed(self) -> None:
        if self._updating_controls or self._selected_panel_id is None:
            return
        if not self.set_panel_span(
            self._selected_panel_id,
            self.row_span_spin.value(),
            self.column_span_spin.value(),
        ):
            self._refresh_controls()

    def _find_panel(self, panel_id: str | None) -> dict | None:
        if panel_id is None:
            return None
        for panel in self._layout["panels"]:
            if panel["id"] == panel_id:
                return panel
        return None

    def _unique_panel_id(self, panel_type: str) -> str:
        if panel_type == PANEL_RESULT_SUMMARY:
            base = "summary"
        else:
            base = panel_type
        used = {panel["id"] for panel in self._layout["panels"]}
        if base not in used:
            return base
        index = 2
        while f"{base}_{index}" in used:
            index += 1
        return f"{base}_{index}"

    def _first_empty_cell(self) -> tuple[int, int] | None:
        for row in range(self._layout["rows"]):
            for column in range(self._layout["columns"]):
                panel = {
                    "id": "__candidate__",
                    "type": PANEL_RESULT_SUMMARY,
                    "row": row,
                    "column": column,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": CHANNEL_NONE,
                }
                if self._can_place(panel):
                    return row, column
        return None

    def _can_place(self, panel: dict, *, ignore_panel_id: str | None = None) -> bool:
        if panel["row"] < 0 or panel["column"] < 0:
            return False
        if panel["row_span"] < 1 or panel["column_span"] < 1:
            return False
        if panel["row"] + panel["row_span"] > self._layout["rows"]:
            return False
        if panel["column"] + panel["column_span"] > self._layout["columns"]:
            return False
        for existing_panel in self._layout["panels"]:
            if existing_panel["id"] == ignore_panel_id:
                continue
            if self._panels_overlap(existing_panel, panel):
                return False
        return True

    @staticmethod
    def _panels_overlap(first: dict, second: dict) -> bool:
        return (
            first["row"] < second["row"] + second["row_span"]
            and second["row"] < first["row"] + first["row_span"]
            and first["column"] < second["column"] + second["column_span"]
            and second["column"] < first["column"] + first["column_span"]
        )

    @staticmethod
    def _channels_for_family(family: str) -> list[str]:
        if family == PANEL_FAMILY_SUMMARY:
            return [CHANNEL_NONE]
        if family == PANEL_FAMILY_ACOUSTIC:
            return [CHANNEL_PRIMARY_ACOUSTIC, CHANNEL_SELECTED_1, CHANNEL_SELECTED_2]
        if family == PANEL_FAMILY_VIBRATION:
            return [CHANNEL_VIBRATION, CHANNEL_SELECTED_1, CHANNEL_SELECTED_2]
        return []

    @staticmethod
    def _set_combo_data(combo: QComboBox, data) -> None:
        blocker = QSignalBlocker(combo)
        try:
            index = combo.findData(data)
            if index >= 0:
                combo.setCurrentIndex(index)
        finally:
            del blocker

    @staticmethod
    def _set_spin_value(spin: QSpinBox, value: int) -> None:
        blocker = QSignalBlocker(spin)
        try:
            spin.setValue(int(value))
        finally:
            del blocker
