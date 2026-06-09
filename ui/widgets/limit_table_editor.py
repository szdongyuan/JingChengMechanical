from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.service.analysis.limit_tables import (
    LIMIT_KIND_LOWER,
    LIMIT_KIND_UPPER,
    normalize_limit_tables,
)

_RAW_KIND_UNSET = object()
_DANGER_BUTTON_STYLE = (
    "QPushButton {"
    " background: white;"
    " border: 1px solid #ef4444;"
    " color: #ef4444;"
    "}"
)


@dataclass
class _LimitTableBlock:
    group: QWidget
    kind_combo: QComboBox
    table: QTableWidget
    add_point_button: QPushButton
    insert_point_button: QPushButton
    delete_point_button: QPushButton
    delete_table_button: QPushButton
    raw_kind: Any = _RAW_KIND_UNSET


class _NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class LimitTableEditor(QWidget):
    limitsChanged = pyqtSignal()

    def __init__(self, *, object_name: str, label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self._label = label
        self._prefix = self._prefix_from_object_name(object_name)
        self._blocks: list[_LimitTableBlock] = []
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        add_row = QWidget()
        add_layout = QHBoxLayout(add_row)
        add_layout.setContentsMargins(0, 0, 0, 0)
        add_layout.setSpacing(6)

        self._add_table_button = QPushButton("添加限制表")
        self._add_table_button.setObjectName(f"{self._prefix}_add_limit_table")
        self._add_table_button.clicked.connect(self.add_limit_table)
        add_layout.addWidget(self._add_table_button, 0)
        add_layout.addStretch(1)
        root.addWidget(add_row)

        self._tables_container = QWidget()
        self._tables_layout = QVBoxLayout(self._tables_container)
        self._tables_layout.setContentsMargins(0, 0, 0, 0)
        self._tables_layout.setSpacing(8)
        root.addWidget(self._tables_container)
        root.addStretch(1)

    def set_limits(self, raw_limits) -> None:
        self._loading = True
        try:
            self._clear_blocks()
            try:
                tables = normalize_limit_tables(raw_limits, label=self._label)
            except ValueError:
                self._render_raw_limits(raw_limits)
                return

            for table in tables:
                self._add_table(
                    kind=table.kind,
                    points=[(point.freq, point.limit) for point in table.points],
                    emit_changed=False,
                )
        finally:
            self._loading = False

    def limits(self) -> list[dict]:
        grouped_limits: list[dict] = []
        for block in self._blocks:
            if block.table.columnCount() == 0:
                continue
            points = [
                {
                    "freq": self._cell_value(block.table, 0, column),
                    "limit": self._cell_value(block.table, 1, column),
                }
                for column in range(block.table.columnCount())
            ]
            grouped_limits.append(
                {
                    "kind": self._collected_kind(block),
                    "points": points,
                }
            )
        return grouped_limits

    def add_limit_table(self) -> None:
        self._add_table(kind=LIMIT_KIND_UPPER, points=None, emit_changed=True)

    def _add_table(
        self,
        *,
        kind: str,
        points: Iterable[tuple[Any, Any]] | None,
        emit_changed: bool,
        raw_kind: Any = _RAW_KIND_UNSET,
    ) -> None:
        group = QWidget()
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(4)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        header_layout.addWidget(QLabel("类型"), 0)

        kind_combo = _NoWheelComboBox()
        kind_combo.addItem("上限", LIMIT_KIND_UPPER)
        kind_combo.addItem("下限", LIMIT_KIND_LOWER)
        if raw_kind is not _RAW_KIND_UNSET:
            kind_combo.addItem(f"无效: {raw_kind}", raw_kind)
            kind_combo.setCurrentIndex(kind_combo.count() - 1)
        else:
            combo_kind = (
                kind
                if kind in {LIMIT_KIND_UPPER, LIMIT_KIND_LOWER}
                else LIMIT_KIND_UPPER
            )
            kind_combo.setCurrentIndex(max(0, kind_combo.findData(combo_kind)))
        header_layout.addWidget(kind_combo, 0)
        header_layout.addStretch(1)

        delete_table_button = QPushButton("删除表")
        _apply_danger_button_style(delete_table_button)
        header_layout.addWidget(delete_table_button, 0)
        group_layout.addWidget(header)

        point_table = self._create_point_table()
        group_layout.addWidget(point_table)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(6)

        add_point_button = QPushButton("添加频点")
        insert_point_button = QPushButton("插入频点")
        delete_point_button = QPushButton("删除选中")
        _apply_danger_button_style(delete_point_button)
        button_layout.addWidget(add_point_button)
        button_layout.addWidget(insert_point_button)
        button_layout.addWidget(delete_point_button)
        button_layout.addStretch(1)
        group_layout.addWidget(button_row)

        block = _LimitTableBlock(
            group=group,
            kind_combo=kind_combo,
            table=point_table,
            add_point_button=add_point_button,
            insert_point_button=insert_point_button,
            delete_point_button=delete_point_button,
            delete_table_button=delete_table_button,
            raw_kind=raw_kind,
        )
        self._blocks.append(block)
        self._tables_layout.addWidget(group)
        self._renumber_blocks()

        kind_combo.currentIndexChanged.connect(
            lambda _index, current=block: self._kind_changed(current)
        )
        add_point_button.clicked.connect(
            lambda _checked=False, current=block: self._append_point(current)
        )
        insert_point_button.clicked.connect(
            lambda _checked=False, current=block: self._insert_point_after_selection(current)
        )
        delete_point_button.clicked.connect(
            lambda _checked=False, current=block: self._delete_selected_points(current)
        )
        delete_table_button.clicked.connect(
            lambda _checked=False, current=block: self._delete_table(current)
        )

        seed_points = list(points) if points is not None else self._default_points(kind)
        for freq, limit in seed_points:
            self._append_point_values(block, freq, limit)

        if emit_changed:
            self._emit_limits_changed()

    def _render_raw_limits(self, raw_limits) -> None:
        if not isinstance(raw_limits, list):
            self._add_table(
                kind=LIMIT_KIND_UPPER,
                points=[("", raw_limits)],
                emit_changed=False,
            )
            return

        legacy_rows: list[dict] = []
        grouped_rows: list[dict] = []
        unknown_rows: list[Any] = []
        mixed_shape = False
        for item in raw_limits:
            shape = self._raw_item_shape(item)
            if shape == "legacy":
                legacy_rows.append(item)
            elif shape == "grouped":
                grouped_rows.append(item)
            else:
                unknown_rows.append(item)
                mixed_shape = mixed_shape or shape == "mixed"

        if legacy_rows:
            self._render_raw_legacy_limits(legacy_rows)
        if grouped_rows:
            self._render_raw_grouped_limits(grouped_rows)
        for item in unknown_rows:
            self._add_table(
                kind=LIMIT_KIND_UPPER,
                points=[("", item)],
                emit_changed=False,
            )
        if mixed_shape or (legacy_rows and grouped_rows):
            self._add_table(
                kind=LIMIT_KIND_UPPER,
                points=[("", "mixed-limit-shape")],
                emit_changed=False,
            )

    def _render_raw_legacy_limits(self, rows: Iterable[dict]) -> None:
        lower_points: list[tuple[Any, Any]] = []
        upper_points: list[tuple[Any, Any]] = []
        for row in rows:
            freq = row.get("freq", "")
            lower_points.append((freq, row.get("lower", "")))
            upper_points.append((freq, row.get("upper", "")))

        if lower_points:
            self._add_table(
                kind=LIMIT_KIND_LOWER,
                points=lower_points,
                emit_changed=False,
            )
        if upper_points:
            self._add_table(
                kind=LIMIT_KIND_UPPER,
                points=upper_points,
                emit_changed=False,
            )

    def _render_raw_grouped_limits(self, rows: Iterable[dict]) -> None:
        for row in rows:
            raw_kind = row.get("kind")
            visible_kind = (
                raw_kind
                if raw_kind in {LIMIT_KIND_UPPER, LIMIT_KIND_LOWER}
                else LIMIT_KIND_UPPER
            )
            raw_kind_override = (
                _RAW_KIND_UNSET
                if raw_kind in {LIMIT_KIND_UPPER, LIMIT_KIND_LOWER}
                else raw_kind
            )
            raw_points = row.get("points")
            if isinstance(raw_points, list):
                points = self._raw_grouped_points(raw_points)
            else:
                points = [("", raw_points)]
            self._add_table(
                kind=visible_kind,
                points=points,
                emit_changed=False,
                raw_kind=raw_kind_override,
            )

    @staticmethod
    def _raw_grouped_points(raw_points: Iterable[Any]) -> list[tuple[Any, Any]]:
        points: list[tuple[Any, Any]] = []
        for point in raw_points:
            if isinstance(point, dict):
                points.append((point.get("freq", ""), point.get("limit", "")))
            else:
                points.append(("", point))
        return points

    @staticmethod
    def _raw_item_shape(item: Any) -> str:
        if not isinstance(item, dict):
            return "unknown"

        fields = set(item)
        legacy_fields = {"freq", "lower", "upper"}
        grouped_fields = {"kind", "points"}
        has_legacy = bool(legacy_fields & fields)
        has_grouped = bool(grouped_fields & fields)
        if has_legacy and has_grouped:
            return "mixed"
        if has_legacy:
            return "legacy"
        if has_grouped:
            return "grouped"
        return "unknown"

    def _create_point_table(self) -> QTableWidget:
        table = QTableWidget()
        table.setRowCount(2)
        table.setVerticalHeaderLabels(["频率 (Hz)", "限值 (dB)"])
        table.horizontalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectColumns)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.horizontalHeader().setDefaultSectionSize(72)
        table.horizontalHeader().setMinimumSectionSize(56)
        table.verticalHeader().setDefaultSectionSize(30)
        table.verticalHeader().setFixedWidth(70)
        table.setFixedHeight(2 * 30 + 2)
        table.setStyleSheet(
            "QTableWidget { font-size: 12px; }"
            "QHeaderView::section { font-size: 12px; padding: 1px 2px; }"
            "QTableView::item { padding: 1px 2px; }"
        )
        table.itemChanged.connect(self._emit_limits_changed)
        return table

    def _append_point(self, block: _LimitTableBlock) -> None:
        if block.table.columnCount() == 0:
            freq = 1000.0
            limit = self._default_limit(block.kind_combo.currentData())
        else:
            last_col = block.table.columnCount() - 1
            last_freq = self._finite_cell_number(block.table, 0, last_col)
            last_limit = self._finite_cell_number(block.table, 1, last_col)
            freq = (last_freq + 1000.0) if last_freq is not None else 1000.0
            limit = (
                last_limit
                if last_limit is not None
                else self._default_limit(block.kind_combo.currentData())
            )
        self._append_point_values(block, freq, limit)
        self._select_column(block.table, block.table.columnCount() - 1)
        self._emit_limits_changed()

    def _insert_point_after_selection(self, block: _LimitTableBlock) -> None:
        selected_columns = self._selected_columns(block.table)
        if not selected_columns:
            self._append_point(block)
            return

        source_col = selected_columns[-1]
        insert_col = source_col + 1
        freq = self._finite_cell_number(block.table, 0, source_col)
        selected_freq = self._cell_value(block.table, 0, source_col)
        selected_limit = self._cell_value(block.table, 1, source_col)
        next_freq = (
            self._finite_cell_number(block.table, 0, insert_col)
            if insert_col < block.table.columnCount()
            else None
        )

        if (
            freq is not None
            and freq > 0
            and next_freq is not None
            and next_freq > 0
            and next_freq > freq
        ):
            insert_freq = (freq + next_freq) / 2.0
        else:
            insert_freq = selected_freq

        was_blocked = block.table.blockSignals(True)
        try:
            block.table.insertColumn(insert_col)
            self._set_point_column(block.table, insert_col, insert_freq, selected_limit)
        finally:
            block.table.blockSignals(was_blocked)
        self._select_column(block.table, insert_col)
        self._emit_limits_changed()

    def _delete_selected_points(self, block: _LimitTableBlock) -> None:
        was_blocked = block.table.blockSignals(True)
        try:
            for column in reversed(self._selected_columns(block.table)):
                block.table.removeColumn(column)
        finally:
            block.table.blockSignals(was_blocked)
        self._emit_limits_changed()

    def _delete_table(self, block: _LimitTableBlock) -> None:
        if block not in self._blocks:
            return
        self._blocks.remove(block)
        self._tables_layout.removeWidget(block.group)
        block.group.setParent(None)
        block.group.deleteLater()
        self._renumber_blocks()
        self._emit_limits_changed()

    def _kind_changed(self, block: _LimitTableBlock) -> None:
        if block.kind_combo.currentData() in {LIMIT_KIND_UPPER, LIMIT_KIND_LOWER}:
            self._remove_transitional_raw_kind(block)
        self._emit_limits_changed()

    @staticmethod
    def _remove_transitional_raw_kind(block: _LimitTableBlock) -> None:
        if block.raw_kind is _RAW_KIND_UNSET:
            return

        raw_kind = block.raw_kind
        was_blocked = block.kind_combo.blockSignals(True)
        try:
            index = block.kind_combo.findData(raw_kind)
            while index != -1:
                block.kind_combo.removeItem(index)
                index = block.kind_combo.findData(raw_kind)
        finally:
            block.kind_combo.blockSignals(was_blocked)
        block.raw_kind = _RAW_KIND_UNSET

    def _emit_limits_changed(self) -> None:
        if not self._loading:
            self.limitsChanged.emit()

    def _append_point_values(
        self,
        block: _LimitTableBlock,
        freq: Any,
        limit: Any,
    ) -> None:
        column = block.table.columnCount()
        was_blocked = block.table.blockSignals(True)
        try:
            block.table.insertColumn(column)
            self._set_point_column(block.table, column, freq, limit)
        finally:
            block.table.blockSignals(was_blocked)

    @staticmethod
    def _set_point_column(
        table: QTableWidget,
        column: int,
        freq: Any,
        limit: Any,
    ) -> None:
        for row, value in enumerate((freq, limit)):
            item = QTableWidgetItem(_format_cell(value))
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
            )
            table.setItem(row, column, item)

    def _clear_blocks(self) -> None:
        while self._blocks:
            block = self._blocks.pop()
            self._tables_layout.removeWidget(block.group)
            block.group.setParent(None)
            block.group.deleteLater()

    def _renumber_blocks(self) -> None:
        for index, block in enumerate(self._blocks):
            block.group.setObjectName(f"{self._prefix}_limit_table_group_{index}")
            block.kind_combo.setObjectName(f"{self._prefix}_limit_kind_{index}")
            block.table.setObjectName(f"{self._prefix}_limit_points_{index}")
            block.add_point_button.setObjectName(
                f"{self._prefix}_add_limit_point_{index}"
            )
            block.insert_point_button.setObjectName(
                f"{self._prefix}_insert_limit_point_{index}"
            )
            block.delete_point_button.setObjectName(
                f"{self._prefix}_delete_limit_point_{index}"
            )
            block.delete_table_button.setObjectName(
                f"{self._prefix}_delete_limit_table_{index}"
            )

    @staticmethod
    def _default_points(kind: str) -> list[tuple[float, float]]:
        limit = LimitTableEditor._default_limit(kind)
        return [(1000.0, limit), (2000.0, limit)]

    @staticmethod
    def _default_limit(kind: str) -> float:
        return 0.0 if kind == LIMIT_KIND_LOWER else 100.0

    @staticmethod
    def _selected_columns(table: QTableWidget) -> list[int]:
        return sorted({index.column() for index in table.selectedIndexes()})

    @staticmethod
    def _select_column(table: QTableWidget, column: int) -> None:
        table.clearSelection()
        if column >= 0:
            table.selectColumn(column)

    @staticmethod
    def _cell_value(table: QTableWidget, row: int, column: int):
        item = table.item(row, column)
        text = item.text() if item is not None else ""
        value = _finite_number_from_text(text)
        return value if value is not None else text

    @staticmethod
    def _collected_kind(block: _LimitTableBlock):
        if block.raw_kind is not _RAW_KIND_UNSET:
            return block.raw_kind
        return block.kind_combo.currentData() or LIMIT_KIND_UPPER

    @staticmethod
    def _finite_cell_number(
        table: QTableWidget,
        row: int,
        column: int,
    ) -> float | None:
        item = table.item(row, column)
        return _finite_number_from_text(item.text() if item is not None else None)

    @staticmethod
    def _prefix_from_object_name(object_name: str) -> str:
        suffix = "_limit_table_editor"
        if object_name.endswith(suffix):
            return object_name[: -len(suffix)]
        return object_name


def _apply_danger_button_style(button: QPushButton) -> None:
    button.setProperty("danger", True)
    button.setStyleSheet(_DANGER_BUTTON_STYLE)


def _finite_number_from_text(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _format_cell(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
