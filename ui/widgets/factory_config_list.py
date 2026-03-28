from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QDrag, QDropEvent, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class _FactoryConfigRowWidget(QWidget):
    def __init__(
        self,
        *,
        config_name: str,
        secondary_text: str,
        checked: bool,
        primary: bool,
        locked: bool,
        on_toggled,
    ):
        super().__init__()
        self._on_toggled = on_toggled
        self._locked = locked

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(checked)
        self.checkbox.toggled.connect(self._handle_toggled)
        layout.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        self.lbl_name = QLabel(config_name)
        text_layout.addWidget(self.lbl_name)

        self.lbl_secondary = QLabel(secondary_text)
        self.lbl_secondary.setVisible(bool(secondary_text))
        self.lbl_secondary.setStyleSheet("color: #64748b; font-size: 11px;")
        text_layout.addWidget(self.lbl_secondary)
        layout.addLayout(text_layout, 1)

        self.lbl_primary = QLabel()
        self.lbl_primary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_primary.setStyleSheet(
            "QLabel { background: #dbeafe; color: #1d4ed8; border-radius: 9px; "
            "padding: 2px 8px; font-size: 11px; font-weight: 600; }"
        )
        layout.addWidget(self.lbl_primary, 0, Qt.AlignmentFlag.AlignTop)

        self.set_primary(primary)
        self.set_locked(locked)

    def set_primary(self, primary: bool) -> None:
        self.lbl_primary.setText("Primary" if primary else "")
        self.lbl_primary.setVisible(primary)
        self.lbl_name.setStyleSheet(
            "font-weight: 700; color: #0f172a;" if primary else "color: #0f172a;"
        )
        self.setStyleSheet(
            "QWidget { background: #eff6ff; border-radius: 8px; }"
            if primary
            else "QWidget { background: transparent; }"
        )

    def set_checked(self, checked: bool) -> None:
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(checked)
        self.checkbox.blockSignals(False)

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.checkbox.setEnabled(not locked)

    def badge_text(self) -> str:
        return self.lbl_primary.text()

    def _handle_toggled(self, checked: bool) -> None:
        if self._locked:
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(not checked)
            self.checkbox.blockSignals(False)
            return
        self._on_toggled(checked)


class FactoryConfigListWidget(QListWidget):
    enabled_order_changed = pyqtSignal(tuple)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows_by_id: dict[int, dict] = {}
        self._base_order: list[int] = []
        self._enabled_order: list[int] = []
        self._locked = False

        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setAlternatingRowColors(False)
        self.setStyleSheet(
            "QListWidget { background: #f8fafc; border: 1px solid #e2e8f0; "
            "border-radius: 8px; padding: 4px; }"
            "QListWidget::item { border: 1px solid transparent; margin: 2px 0; "
            "padding: 0; }"
            "QListWidget::item:selected { background: #e2e8f0; }"
        )

    def set_rows(
        self,
        *,
        rows: list[dict],
        enabled_config_ids: tuple[int, ...] | list[int] = (),
    ) -> None:
        self._rows_by_id = {}
        self._base_order = []
        for row in rows:
            config_id = row.get("config_id")
            if not isinstance(config_id, int):
                continue
            self._rows_by_id[config_id] = dict(row)
            self._base_order.append(config_id)
        self._enabled_order = self._normalize_enabled_order(enabled_config_ids)
        self._rebuild()

    def sync_enabled_config_ids(self, config_ids: tuple[int, ...] | list[int]) -> None:
        self._enabled_order = self._normalize_enabled_order(config_ids)
        self._rebuild()

    def enabled_config_ids(self) -> tuple[int, ...]:
        return tuple(self._enabled_order)

    def visual_config_order(self) -> tuple[int, ...]:
        enabled = list(self._enabled_order)
        enabled_set = set(enabled)
        unchecked = [config_id for config_id in self._base_order if config_id not in enabled_set]
        return tuple(enabled + unchecked)

    def primary_config_id(self) -> int | None:
        if not self._enabled_order:
            return None
        return self._enabled_order[0]

    def primary_row_badge_text(self) -> str:
        primary_id = self.primary_config_id()
        if primary_id is None:
            return ""
        widget = self._row_widget(primary_id)
        if widget is None:
            return ""
        return widget.badge_text()

    def set_checked(self, config_id: int, checked: bool, *, emit_signal: bool = True) -> None:
        if config_id not in self._rows_by_id:
            return
        if emit_signal and self._locked:
            return
        next_enabled = list(self._enabled_order)
        if checked:
            if config_id not in next_enabled:
                next_enabled.append(config_id)
        elif config_id in next_enabled:
            next_enabled.remove(config_id)
        if next_enabled == self._enabled_order:
            return
        self._enabled_order = next_enabled
        self._rebuild()
        if emit_signal:
            self.enabled_order_changed.emit(tuple(self._enabled_order))

    def move_checked_config(
        self,
        config_id: int,
        *,
        before_config_id: int | None,
        emit_signal: bool = True,
    ) -> None:
        if config_id not in self._enabled_order:
            return
        if emit_signal and self._locked:
            return
        next_enabled = [current_id for current_id in self._enabled_order if current_id != config_id]
        if before_config_id in next_enabled:
            next_enabled.insert(next_enabled.index(before_config_id), config_id)
        else:
            next_enabled.append(config_id)
        if next_enabled == self._enabled_order:
            return
        self._enabled_order = next_enabled
        self._rebuild()
        if emit_signal:
            self.enabled_order_changed.emit(tuple(self._enabled_order))

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.setDragDropMode(
            QAbstractItemView.DragDropMode.NoDragDrop
            if locked
            else QAbstractItemView.DragDropMode.InternalMove
        )
        for row_index in range(self.count()):
            item = self.item(row_index)
            config_id = item.data(Qt.ItemDataRole.UserRole)
            widget = self.itemWidget(item)
            if isinstance(widget, _FactoryConfigRowWidget):
                widget.set_locked(locked)
            if isinstance(config_id, int):
                item.setFlags(self._item_flags(config_id))

    def is_locked(self) -> bool:
        return self._locked

    def dropEvent(self, event: QDropEvent) -> None:
        if self._locked:
            event.ignore()
            return

        dragged_config_id = self._dragged_config_id()
        if dragged_config_id not in self._enabled_order:
            event.ignore()
            return

        insertion_row = self._drop_insertion_row(event)
        if insertion_row is None:
            event.ignore()
            return

        before_config_id = self._before_config_id_for_insertion_row(
            dragged_config_id,
            insertion_row,
        )
        previous_enabled = tuple(self._enabled_order)
        self.move_checked_config(
            dragged_config_id,
            before_config_id=before_config_id,
            emit_signal=False,
        )
        if tuple(self._enabled_order) != previous_enabled:
            self.enabled_order_changed.emit(tuple(self._enabled_order))
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        indexes = self._selected_draggable_indexes()
        if not indexes:
            return
        dragged_item = self.itemFromIndex(indexes[0])
        if dragged_item is None:
            return

        mime_data = self.model().mimeData(indexes)
        if mime_data is None:
            return

        drag = QDrag(self)
        drag.setMimeData(mime_data)

        preview = self._drag_preview_pixmap(dragged_item)
        if not preview.isNull():
            drag.setPixmap(preview)
            row_rect = self.visualItemRect(dragged_item)
            if not row_rect.isNull():
                drag.setHotSpot(self.viewport().mapFromGlobal(QCursor.pos()) - row_rect.topLeft())

        if not supported_actions & Qt.DropAction.MoveAction:
            return
        drag.exec(Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)

    def _normalize_enabled_order(
        self,
        enabled_config_ids: tuple[int, ...] | list[int],
    ) -> list[int]:
        seen: set[int] = set()
        enabled: list[int] = []
        for config_id in enabled_config_ids:
            if (
                not isinstance(config_id, int)
                or config_id in seen
                or config_id not in self._rows_by_id
            ):
                continue
            seen.add(config_id)
            enabled.append(config_id)
        return enabled

    def _rebuild(self) -> None:
        self.clear()
        for config_id in self.visual_config_order():
            row = self._rows_by_id[config_id]
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, config_id)
            item.setFlags(self._item_flags(config_id))
            self.addItem(item)
            widget = _FactoryConfigRowWidget(
                config_name=row.get("config_name") or f"Config {config_id}",
                secondary_text=row.get("secondary_text") or row.get("summary_text") or "",
                checked=config_id in self._enabled_order,
                primary=(config_id == self.primary_config_id()),
                locked=self._locked,
                on_toggled=lambda checked, current_config_id=config_id: self.set_checked(
                    current_config_id,
                    checked,
                    emit_signal=True,
                ),
            )
            item.setSizeHint(widget.sizeHint())
            self.setItemWidget(item, widget)

    def _item_flags(self, config_id: int) -> Qt.ItemFlag:
        flags = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        if not self._locked and config_id in self._enabled_order:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def _row_widget(self, config_id: int) -> _FactoryConfigRowWidget | None:
        for row_index in range(self.count()):
            item = self.item(row_index)
            if item.data(Qt.ItemDataRole.UserRole) != config_id:
                continue
            widget = self.itemWidget(item)
            if isinstance(widget, _FactoryConfigRowWidget):
                return widget
        return None

    def _selected_draggable_indexes(self) -> list:
        indexes = []
        for index in self.selectedIndexes():
            if not index.isValid():
                continue
            item = self.itemFromIndex(index)
            if item is None or not item.flags() & Qt.ItemFlag.ItemIsDragEnabled:
                continue
            indexes.append(index)
        return indexes

    def _drag_preview_pixmap(
        self,
        item: QListWidgetItem | None = None,
    ) -> QPixmap:
        dragged_item = item or self._dragged_item()
        if dragged_item is None:
            return QPixmap()
        row_widget = self.itemWidget(dragged_item)
        if row_widget is None:
            return QPixmap()
        return row_widget.grab(row_widget.rect())

    def _dragged_item(self) -> QListWidgetItem | None:
        selected_items = self.selectedItems()
        if selected_items:
            return selected_items[0]
        return self.currentItem()

    def _dragged_config_id(self) -> int | None:
        dragged_item = self._dragged_item()
        if dragged_item is None:
            return None
        config_id = dragged_item.data(Qt.ItemDataRole.UserRole)
        return config_id if isinstance(config_id, int) else None

    def _drop_insertion_row(self, event: QDropEvent) -> int | None:
        position = self._drop_position(event)
        if position is None:
            return None
        for row_index in range(self.count()):
            item = self.item(row_index)
            if position.y() < self.visualItemRect(item).center().y():
                return row_index
        return self.count()

    def _drop_position(self, event: QDropEvent):
        position = getattr(event, "position", None)
        if callable(position):
            point = position()
            to_point = getattr(point, "toPoint", None)
            return to_point() if callable(to_point) else point
        pos = getattr(event, "pos", None)
        if callable(pos):
            return pos()
        return None

    def _before_config_id_for_insertion_row(
        self,
        dragged_config_id: int,
        insertion_row: int,
    ) -> int | None:
        visual_order = list(self.visual_config_order())
        if dragged_config_id not in visual_order:
            return None
        dragged_row = visual_order.index(dragged_config_id)
        visual_without_dragged = [
            config_id for config_id in visual_order if config_id != dragged_config_id
        ]
        if insertion_row > dragged_row:
            insertion_row -= 1
        insertion_row = max(0, min(insertion_row, len(visual_without_dragged)))

        enabled_without_dragged = set(self._enabled_order)
        enabled_without_dragged.discard(dragged_config_id)
        for config_id in visual_without_dragged[insertion_row:]:
            if config_id in enabled_without_dragged:
                return config_id
        return None
