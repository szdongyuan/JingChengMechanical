from __future__ import annotations

import csv
from dataclasses import dataclass
import math
from typing import ClassVar, Literal

from pyqtgraph.exporters import ImageExporter
from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMenu,
    QMessageBox,
    QRubberBand,
    QWidget,
)

_DragOperation = Literal["pan", "box_zoom"]
CsvExportScope = Literal["all", "visible"]
ContextMenuAction = Literal[
    "reset_view",
    "save_png",
    "copy_image",
    "export_all_csv",
    "export_visible_csv",
]
CONTEXT_MENU_ACTIONS: tuple[tuple[str, str], ...] = (
    ("reset_view", "重置视图"),
    ("save_png", "保存图片为 PNG"),
    ("copy_image", "复制图片"),
    ("export_all_csv", "导出全部 CSV"),
    ("export_visible_csv", "导出可见范围 CSV"),
)
_MIN_VIEW_SPAN = 1e-9
_WHEEL_ZOOM_BASE = 0.85


def _is_deleted(wrapped) -> bool:
    try:
        return sip.isdeleted(wrapped)
    except RuntimeError:
        return True


@dataclass(frozen=True)
class ViewRange:
    x_min: float
    x_max: float
    y_min: float
    y_max: float


@dataclass(frozen=True)
class CsvTable:
    headers: list[str]
    rows: list[list[str]]
    x_values: list[float]


@dataclass
class _DragState:
    operation: _DragOperation
    start_pos: QPoint
    last_pos: QPoint
    start_view: QPointF
    last_view: QPointF


class ChartInteractionController(QObject):
    _active_context_menu: ClassVar[QMenu | None] = None

    def __init__(
        self,
        chart,
        *,
        parent: QWidget | None = None,
        pass_through_left_clicks: bool = False,
        pass_through_right_clicks: bool = False,
        context_menu_enabled: bool = True,
        context_menu_actions: tuple[ContextMenuAction, ...] | None = None,
    ):
        super().__init__(parent or chart)
        self._chart = chart
        self._chart_widget: QWidget | None = chart
        self._plot_widget: QWidget | None = None
        self._viewport: QWidget | None = None
        self._full_view_range: ViewRange | None = None
        self._drag_state: _DragState | None = None
        self._wheel_zoom_enabled = True
        self._left_box_zoom_enabled = True
        self._middle_pan_enabled = True
        self._pass_through_left_clicks = bool(pass_through_left_clicks)
        self._pass_through_right_clicks = bool(pass_through_right_clicks)
        self._context_menu_enabled = bool(context_menu_enabled)
        self._context_menu_actions = (
            tuple(action_key for action_key, _label in CONTEXT_MENU_ACTIONS)
            if context_menu_actions is None
            else tuple(context_menu_actions)
        )
        self._context_menu: QMenu | None = None
        viewport = chart.plot_widget.viewport()
        self._viewport = viewport
        self._plot_widget = chart.plot_widget
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, viewport)
        self._rubber_band.hide()
        chart.installEventFilter(self)
        chart.destroyed.connect(self._on_chart_destroyed)
        chart.plot_widget.installEventFilter(self)
        chart.plot_widget.destroyed.connect(self._on_plot_widget_destroyed)
        viewport.setMouseTracking(True)
        viewport.installEventFilter(self)
        viewport.destroyed.connect(self._on_viewport_destroyed)
        self.destroyed.connect(self._detach_event_filter)
        self.reset_button = None

    def menu_action_enabled_states(self) -> dict[str, bool]:
        return {
            "reset_view": True,
            "save_png": True,
            "copy_image": True,
            "export_all_csv": True,
            "export_visible_csv": True,
        }

    def context_menu_action_labels(self) -> list[str]:
        return [
            label
            for action_key, label in CONTEXT_MENU_ACTIONS
            if action_key in self._context_menu_actions
        ]

    def set_left_box_zoom_enabled(self, enabled: bool) -> None:
        self._left_box_zoom_enabled = bool(enabled)
        if not self._left_box_zoom_enabled:
            self._cancel_drag_operation("box_zoom")

    def set_wheel_zoom_enabled(self, enabled: bool) -> None:
        self._wheel_zoom_enabled = bool(enabled)

    def set_middle_pan_enabled(self, enabled: bool) -> None:
        self._middle_pan_enabled = bool(enabled)
        if not self._middle_pan_enabled:
            self._cancel_drag_operation("pan")

    def show_context_menu(self, global_pos) -> None:
        self._close_active_context_menu()
        menu = QMenu(self._chart)
        self._context_menu = menu
        ChartInteractionController._active_context_menu = menu
        menu.aboutToHide.connect(
            lambda menu=menu: self._clear_context_menu_reference(menu)
        )
        enabled_states = self.menu_action_enabled_states()
        for action_key, label in CONTEXT_MENU_ACTIONS:
            if action_key not in self._context_menu_actions:
                continue
            action = menu.addAction(label)
            action.setEnabled(enabled_states.get(action_key, True))
            action.triggered.connect(
                lambda _checked=False, key=action_key: self.trigger_context_menu_action(
                    key
                )
            )
        if hasattr(global_pos, "toPoint"):
            global_pos = global_pos.toPoint()
        menu.popup(global_pos)

    def trigger_context_menu_action(self, action_key: str) -> None:
        if action_key == "reset_view":
            self.reset_view()
        elif action_key == "save_png":
            self.save_png()
        elif action_key == "copy_image":
            self.copy_image()
        elif action_key == "export_all_csv":
            self.export_csv("all")
        elif action_key == "export_visible_csv":
            self.export_csv("visible")
        else:
            raise ValueError(f"Unsupported context menu action: {action_key}")

    def save_png(self) -> None:
        try:
            path = self._choose_png_path()
            if not path:
                return
            self._save_png_to_path(path)
        except Exception as exc:
            self._show_warning("保存图像失败", str(exc))

    def copy_image(self) -> None:
        try:
            pixmap = self._grab_plot_pixmap()
            self._copy_pixmap_to_clipboard(pixmap)
        except Exception as exc:
            self._show_warning("复制图像失败", str(exc))

    def export_csv(self, scope: CsvExportScope) -> None:
        try:
            path = self._choose_csv_path(scope)
            if not path:
                return
            table = self._chart.export_csv_table(scope=scope)
            self._write_csv_to_path(path, table)
        except Exception as exc:
            self._show_warning("导出 CSV 失败", str(exc))

    def domain_range_for_scope(
        self,
        scope: CsvExportScope,
    ) -> tuple[float, float] | None:
        if scope == "all":
            return None
        if scope == "visible":
            x_range, _ = self._chart.plot_item.vb.viewRange()
            domain_range = self._chart.domain_x_range_from_display_range(
                (float(x_range[0]), float(x_range[1]))
            )
            return self._normalized_span(domain_range)
        raise ValueError(f"Unsupported CSV export scope: {scope}")

    def filter_csv_table(self, table: CsvTable, scope: CsvExportScope) -> CsvTable:
        if scope == "all":
            return CsvTable(
                headers=list(table.headers),
                rows=[list(row) for row in table.rows],
                x_values=list(table.x_values),
            )

        domain_range = self.domain_range_for_scope(scope)
        if domain_range is None:
            return CsvTable(headers=list(table.headers), rows=[], x_values=[])

        lower, upper = domain_range
        rows: list[list[str]] = []
        x_values: list[float] = []
        for x_value, row in zip(table.x_values, table.rows, strict=False):
            try:
                numeric_x = float(x_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric_x):
                continue
            if lower <= numeric_x <= upper:
                x_values.append(numeric_x)
                rows.append(list(row))

        return CsvTable(headers=list(table.headers), rows=rows, x_values=x_values)

    def refresh_full_view(self) -> None:
        x_range, y_range = self._chart.plot_item.vb.viewRange()
        if len(x_range) != 2 or len(y_range) != 2:
            return
        values = (x_range[0], x_range[1], y_range[0], y_range[1])
        if not all(math.isfinite(float(value)) for value in values):
            return
        self._full_view_range = ViewRange(
            float(x_range[0]),
            float(x_range[1]),
            float(y_range[0]),
            float(y_range[1]),
        )

    def reset_view(self) -> None:
        if self._full_view_range is None:
            return
        self._chart.plot_item.vb.setRange(
            xRange=(self._full_view_range.x_min, self._full_view_range.x_max),
            yRange=(self._full_view_range.y_min, self._full_view_range.y_max),
            padding=0.0,
        )

    def zoom_at_view_point(self, center_x: float, center_y: float, factor: float) -> None:
        if (
            not math.isfinite(float(center_x))
            or not math.isfinite(float(center_y))
            or not math.isfinite(float(factor))
            or factor <= 0.0
        ):
            return

        x_range, y_range = self._chart.plot_item.vb.viewRange()
        x_span = self._normalized_span(tuple(x_range))
        y_span = self._normalized_span(tuple(y_range))
        if x_span is None or y_span is None:
            return

        x_min, x_max = x_span
        y_min, y_max = y_span
        new_x = (
            float(center_x) - (float(center_x) - x_min) * factor,
            float(center_x) + (x_max - float(center_x)) * factor,
        )
        new_y = (
            float(center_y) - (float(center_y) - y_min) * factor,
            float(center_y) + (y_max - float(center_y)) * factor,
        )
        if self._normalized_span(new_x) is None or self._normalized_span(new_y) is None:
            return
        self._chart.plot_item.vb.setRange(xRange=new_x, yRange=new_y, padding=0.0)

    def pan_by_view_delta(self, dx: float, dy: float) -> None:
        if not math.isfinite(float(dx)) or not math.isfinite(float(dy)):
            return
        x_range, y_range = self._chart.plot_item.vb.viewRange()
        self._chart.plot_item.vb.setRange(
            xRange=(float(x_range[0]) + float(dx), float(x_range[1]) + float(dx)),
            yRange=(float(y_range[0]) + float(dy), float(y_range[1]) + float(dy)),
            padding=0.0,
        )

    def zoom_to_view_rect(
        self,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> bool:
        x_span = self._normalized_span(x_range)
        y_span = self._normalized_span(y_range)
        if x_span is None or y_span is None:
            return False
        self._chart.plot_item.vb.setRange(
            xRange=x_span,
            yRange=y_span,
            padding=0.0,
        )
        return True

    def eventFilter(self, watched, event):
        event_type = event.type()
        chart_widget = getattr(self, "_chart_widget", None)
        plot_widget = getattr(self, "_plot_widget", None)
        viewport = getattr(self, "_viewport", None)
        if event_type == QEvent.Type.DeferredDelete and (
            watched is chart_widget or watched is plot_widget or watched is viewport
        ):
            self.teardown()
            return False
        if watched is chart_widget and event_type in {
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        }:
            return False
        if viewport is None:
            return False
        if _is_deleted(viewport):
            self._viewport = None
            return False
        if watched is not viewport:
            return super().eventFilter(watched, event)

        if event_type == QEvent.Type.Wheel:
            return self._handle_wheel_event(event)
        if event_type == QEvent.Type.MouseButtonDblClick:
            return self._handle_double_click_event(event)
        if event_type == QEvent.Type.MouseButtonPress:
            return self._handle_mouse_press_event(event)
        if event_type == QEvent.Type.MouseMove:
            return self._handle_mouse_move_event(event)
        if event_type == QEvent.Type.MouseButtonRelease:
            return self._handle_mouse_release_event(event)
        if event_type == QEvent.Type.Leave:
            self._cancel_drag()
            return False

        return super().eventFilter(watched, event)

    @staticmethod
    def _normalized_span(values: tuple[float, float]) -> tuple[float, float] | None:
        lower = float(values[0])
        upper = float(values[1])
        if not math.isfinite(lower) or not math.isfinite(upper):
            return None
        lower, upper = sorted((lower, upper))
        if upper - lower <= _MIN_VIEW_SPAN:
            return None
        return (lower, upper)

    def _handle_wheel_event(self, event) -> bool:
        if not self._wheel_zoom_enabled:
            return False
        delta_y = int(event.angleDelta().y())
        if delta_y == 0:
            return False
        view_point = self._event_view_point(event)
        if view_point is None:
            return False
        factor = _WHEEL_ZOOM_BASE ** (delta_y / 120.0)
        self.zoom_at_view_point(float(view_point.x()), float(view_point.y()), factor)
        return True

    def _handle_double_click_event(self, event) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self.reset_view()
        return True

    def _handle_mouse_press_event(self, event) -> bool:
        if event.button() == Qt.MouseButton.RightButton:
            if not self._context_menu_enabled:
                return False
            self.show_context_menu(self._event_global_position(event))
            return not self._pass_through_right_clicks
        if event.button() == Qt.MouseButton.MiddleButton:
            if not self._middle_pan_enabled:
                return False
            return self._start_drag(event, "pan")
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._left_box_zoom_enabled:
                return False
            started = self._start_drag(event, "box_zoom")
            if started:
                position = self._event_position(event)
                self._update_rubber_band(position, position)
            return started and not self._pass_through_left_clicks
        return False

    def _handle_mouse_move_event(self, event) -> bool:
        drag_state = self._drag_state
        if drag_state is None:
            return False
        position = self._event_position(event)

        if drag_state.operation == "box_zoom":
            self._update_rubber_band(drag_state.start_pos, position)
            return True

        view_point = self._view_point_for_position(position)
        if view_point is None:
            return True

        if drag_state.operation == "pan":
            self.pan_by_view_delta(
                float(drag_state.last_view.x()) - float(view_point.x()),
                float(drag_state.last_view.y()) - float(view_point.y()),
            )
            drag_state.last_pos = position
            remapped_point = self._view_point_for_position(position)
            drag_state.last_view = remapped_point or view_point
            return True

        return True

    def _handle_mouse_release_event(self, event) -> bool:
        drag_state = self._drag_state
        if drag_state is None:
            return False

        if drag_state.operation == "pan":
            if event.button() != Qt.MouseButton.MiddleButton:
                return False
            self._cancel_drag()
            return True

        if drag_state.operation == "box_zoom":
            if event.button() != Qt.MouseButton.LeftButton:
                return False

            position = self._event_position(event)
            if self._is_below_drag_threshold(drag_state.start_pos, position):
                self._cancel_drag()
                return not self._pass_through_left_clicks

            view_point = self._view_point_for_position(position)
            if view_point is not None:
                self.zoom_to_view_rect(
                    (float(drag_state.start_view.x()), float(view_point.x())),
                    (float(drag_state.start_view.y()), float(view_point.y())),
                )

            self._cancel_drag()
            return True

        return False

    def _start_drag(self, event, operation: _DragOperation) -> bool:
        view_point = self._event_view_point(event)
        if view_point is None:
            return False
        position = self._event_position(event)
        self._drag_state = _DragState(
            operation=operation,
            start_pos=position,
            last_pos=position,
            start_view=view_point,
            last_view=view_point,
        )
        return True

    @staticmethod
    def _is_below_drag_threshold(start_pos: QPoint, end_pos: QPoint) -> bool:
        threshold = QApplication.startDragDistance()
        if threshold <= 0:
            return False
        return (
            abs(int(end_pos.x()) - int(start_pos.x()))
            + abs(int(end_pos.y()) - int(start_pos.y()))
        ) < threshold

    def _update_rubber_band(self, start_pos: QPoint, end_pos: QPoint) -> None:
        rubber_band = getattr(self, "_rubber_band", None)
        if rubber_band is None or _is_deleted(rubber_band):
            return
        rubber_band.setGeometry(QRect(start_pos, end_pos).normalized())
        rubber_band.show()

    def _hide_rubber_band(self) -> None:
        rubber_band = getattr(self, "_rubber_band", None)
        if rubber_band is not None and not _is_deleted(rubber_band):
            rubber_band.hide()

    def _event_position(self, event) -> QPoint:
        position = event.position()
        return QPoint(int(round(position.x())), int(round(position.y())))

    def _event_global_position(self, event) -> QPoint:
        if hasattr(event, "globalPosition"):
            return event.globalPosition().toPoint()
        return event.globalPos()

    def _event_view_point(self, event) -> QPointF | None:
        return self._view_point_for_position(self._event_position(event))

    def _view_point_for_position(self, position: QPoint) -> QPointF | None:
        view_point = self._chart.plot_item.vb.mapDeviceToView(
            QPointF(float(position.x()), float(position.y()))
        )
        if not math.isfinite(float(view_point.x())) or not math.isfinite(
            float(view_point.y())
        ):
            return None
        return QPointF(float(view_point.x()), float(view_point.y()))

    def _cancel_drag(self) -> None:
        self._drag_state = None
        self._hide_rubber_band()

    def _cancel_drag_operation(self, operation: _DragOperation) -> None:
        drag_state = self._drag_state
        if drag_state is None or drag_state.operation != operation:
            return
        self._cancel_drag()

    def _choose_png_path(self) -> str | None:
        path, _selected = QFileDialog.getSaveFileName(
            self._chart,
            "保存图片",
            "chart.png",
            "PNG 图片 (*.png)",
        )
        return self._ensure_suffix(path, ".png")

    def _choose_csv_path(self, scope: CsvExportScope) -> str | None:
        path, _selected = QFileDialog.getSaveFileName(
            self._chart,
            "导出 CSV",
            "chart.csv",
            "CSV 文件 (*.csv)",
        )
        return self._ensure_suffix(path, ".csv")

    def _save_png_to_path(self, path: str) -> None:
        exporter = ImageExporter(self._chart.plot_item)
        if exporter.export(path) is False:
            raise RuntimeError("图像保存失败")

    def _grab_plot_pixmap(self):
        return self._chart.plot_widget.grab()

    def _copy_pixmap_to_clipboard(self, pixmap) -> None:
        QApplication.clipboard().setPixmap(pixmap)

    def _write_csv_to_path(self, path: str, table: CsvTable) -> None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(table.headers)
            writer.writerows(table.rows)

    def _show_warning(self, title: str, text: str) -> None:
        QMessageBox.warning(self._chart, title, text)

    @staticmethod
    def _ensure_suffix(path: str, suffix: str) -> str | None:
        if not path:
            return None
        if path.lower().endswith(suffix.lower()):
            return path
        return f"{path}{suffix}"

    def teardown(self) -> None:
        self._cancel_drag()
        self._close_context_menu()
        self._remove_rubber_band()
        self._detach_event_filter()
        self._chart = None

    def _detach_event_filter(self, *args) -> None:
        chart_widget = getattr(self, "_chart_widget", None)
        viewport = getattr(self, "_viewport", None)
        plot_widget = getattr(self, "_plot_widget", None)
        self._chart_widget = None
        self._viewport = None
        self._plot_widget = None
        for watched in (viewport, plot_widget, chart_widget):
            if watched is None or _is_deleted(watched):
                continue
            try:
                watched.removeEventFilter(self)
            except RuntimeError:
                pass

    def _remove_rubber_band(self) -> None:
        rubber_band = getattr(self, "_rubber_band", None)
        self._rubber_band = None
        if rubber_band is None or _is_deleted(rubber_band):
            return
        try:
            rubber_band.hide()
            rubber_band.deleteLater()
        except RuntimeError:
            pass

    @classmethod
    def _close_active_context_menu(cls) -> None:
        menu = cls._active_context_menu
        if menu is None or _is_deleted(menu):
            cls._active_context_menu = None
            return
        try:
            menu.close()
        except RuntimeError:
            pass

    def _close_context_menu(self) -> None:
        menu = getattr(self, "_context_menu", None)
        self._context_menu = None
        if menu is None or _is_deleted(menu):
            return
        if ChartInteractionController._active_context_menu is menu:
            ChartInteractionController._active_context_menu = None
        try:
            menu.close()
            menu.deleteLater()
        except RuntimeError:
            pass

    def _clear_context_menu_reference(self, menu: QMenu) -> None:
        if getattr(self, "_context_menu", None) is menu:
            self._context_menu = None
        if ChartInteractionController._active_context_menu is menu:
            ChartInteractionController._active_context_menu = None
        if not _is_deleted(menu):
            try:
                menu.deleteLater()
            except RuntimeError:
                pass

    def _on_viewport_destroyed(self, *args) -> None:
        self.teardown()
        self._viewport = None

    def _on_plot_widget_destroyed(self, *args) -> None:
        self.teardown()
        self._plot_widget = None

    def _on_chart_destroyed(self, *args) -> None:
        self.teardown()
        self._chart_widget = None
