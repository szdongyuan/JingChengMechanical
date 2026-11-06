from __future__ import annotations

import weakref

from PyQt6.QtCore import QEvent, QPoint, QRect, QSettings, QSize, QTimer, Qt
from PyQt6.QtGui import QMouseEvent, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _TitleBar(QWidget):
    def __init__(self, overlay: "VideoPreviewOverlay") -> None:
        super().__init__(overlay)
        self._overlay = overlay
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._overlay._begin_move(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._overlay._update_move(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._overlay._finish_move()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _ResizeHandle(QWidget):
    def __init__(self, overlay: "VideoPreviewOverlay") -> None:
        super().__init__(overlay)
        self._overlay = overlay
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeBDiagCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._overlay._begin_resize(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._overlay._update_resize(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._overlay._finish_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().color(self.foregroundRole()), 1.5))
        painter.drawLine(QPoint(16, 2), QPoint(2, 16))
        painter.drawLine(QPoint(16, 7), QPoint(7, 16))
        painter.drawLine(QPoint(16, 12), QPoint(12, 16))


class VideoPreviewOverlay(QFrame):
    SETTINGS_CUSTOMIZED = "ui/video_preview_overlay/customized"
    SETTINGS_GEOMETRY = "ui/video_preview_overlay/geometry"
    NORMAL_MINIMUM = QSize(320, 270)
    SUPPORTED_PAGE_MINIMUM = QSize(200, 80)

    def __init__(
        self,
        parent: QWidget,
        body: QWidget,
        anchor: QWidget,
        *,
        settings=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = body
        self._page_ref = weakref.ref(parent)
        self._anchor = anchor
        self._settings = (
            settings
            if settings is not None
            else QSettings("JingChen", "JingChenMechanical")
        )
        self._customized = False
        self._requested_geometry: QRect | None = None
        self._move_origin: QPoint | None = None
        self._move_start_geometry: QRect | None = None
        self._resize_origin: QPoint | None = None
        self._resize_start_geometry: QRect | None = None
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self.sync_geometry)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 8, 12, 8)
        root_layout.setSpacing(8)

        self.title_bar = _TitleBar(self)
        self.title_bar.setFixedHeight(24)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(0, 0, 18, 0)
        title_layout.setSpacing(8)

        self.title_label = QLabel("视频预览", self.title_bar)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        title_layout.addWidget(self.title_label, 1)

        self.reset_button = QPushButton("恢复默认", self.title_bar)
        self.reset_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.reset_button.setFixedHeight(22)
        self.reset_button.setStyleSheet("padding: 1px 6px;")
        self.reset_button.clicked.connect(self.reset_geometry)
        title_layout.addWidget(self.reset_button, 0)

        self.resize_handle = _ResizeHandle(self)
        root_layout.addWidget(self.title_bar)
        root_layout.addWidget(body, 1)

        self._load_state()
        parent.installEventFilter(self)
        anchor.installEventFilter(self)
        parent.destroyed.connect(self._on_page_destroyed)
        anchor.destroyed.connect(self._on_anchor_destroyed)

    @property
    def is_customized(self) -> bool:
        return self._customized

    @property
    def requested_geometry(self) -> QRect | None:
        if self._requested_geometry is None:
            return None
        return QRect(self._requested_geometry)

    def _default_geometry(self) -> QRect:
        parent = self.parentWidget()
        if parent is None or self._anchor is None:
            return QRect()
        top_left = self._anchor.mapTo(parent, QPoint(0, 0))
        return QRect(top_left, self._anchor.size())

    def _effective_geometry(
        self, requested: QRect, *, enforce_minimum: bool = True
    ) -> QRect:
        parent = self.parentWidget()
        if (
            parent is None
            or parent.width() <= 0
            or parent.height() <= 0
            or requested.isEmpty()
        ):
            return QRect()

        available = parent.rect()
        if (
            available.width() < self.SUPPORTED_PAGE_MINIMUM.width()
            or available.height() < self.SUPPORTED_PAGE_MINIMUM.height()
        ):
            return QRect(available)

        if (
            (available.width() < self.NORMAL_MINIMUM.width()
             or available.height() < self.NORMAL_MINIMUM.height())
            and available.contains(requested)
            and self._can_fit_header(requested)
        ):
            # A usable partial default remains valid after resizing. Inflating
            # it here would move the fixed edges on release or settings reload.
            return QRect(requested)

        # On undersized pages a completed resize can legitimately leave space
        # beside the fixed left/bottom edges. Do not inflate it on release.
        minimum_width = (
            self.NORMAL_MINIMUM.width()
            if available.width() >= self.NORMAL_MINIMUM.width()
            else self.SUPPORTED_PAGE_MINIMUM.width()
        )
        minimum_height = (
            self.NORMAL_MINIMUM.height()
            if available.height() >= self.NORMAL_MINIMUM.height()
            else self.SUPPORTED_PAGE_MINIMUM.height()
        )
        if not enforce_minimum:
            minimum_width = minimum_height = 1
        width = min(max(requested.width(), minimum_width), available.width())
        height = min(max(requested.height(), minimum_height), available.height())
        x = min(
            max(requested.x(), available.left()),
            available.right() - width + 1,
        )
        y = min(
            max(requested.y(), available.top()),
            available.bottom() - height + 1,
        )
        return QRect(x, y, width, height)

    def _can_fit_header(self, rect: QRect) -> bool:
        margins = self.layout().contentsMargins()
        header_width = (
            self.title_bar.minimumSizeHint().width()
            + margins.left() + margins.right() + 2 * self.frameWidth()
        )
        header_height = (
            self.title_bar.height()
            + margins.top() + margins.bottom() + 2 * self.frameWidth()
        )
        return rect.width() >= header_width and rect.height() >= header_height

    def _effective_default_geometry(self) -> QRect:
        requested = self._default_geometry()
        parent = self.parentWidget()
        if parent is None or requested.isEmpty():
            return QRect()
        if (
            parent.width() < self.SUPPORTED_PAGE_MINIMUM.width()
            or parent.height() < self.SUPPORTED_PAGE_MINIMUM.height()
        ):
            return self._effective_geometry(requested)

        intersection = requested.intersected(parent.rect())
        if self._can_fit_header(intersection):
            return intersection
        # An off-page or unusable anchor must not strand the header controls.
        return self._effective_geometry(requested)

    def _effective_top_right_resize(self, requested: QRect) -> QRect:
        parent = self.parentWidget()
        if parent is None or parent.width() <= 0 or parent.height() <= 0:
            return QRect()

        available = parent.rect()
        if (
            available.width() < self.SUPPORTED_PAGE_MINIMUM.width()
            or available.height() < self.SUPPORTED_PAGE_MINIMUM.height()
        ):
            return QRect(available)

        fixed_left = min(max(requested.left(), available.left()), available.right())
        fixed_bottom = min(
            max(requested.bottom(), available.top()), available.bottom()
        )
        maximum_width = available.right() - fixed_left + 1
        maximum_height = fixed_bottom - available.top() + 1
        minimum_width = min(self.NORMAL_MINIMUM.width(), maximum_width)
        minimum_height = min(self.NORMAL_MINIMUM.height(), maximum_height)

        right = min(
            max(requested.right(), fixed_left + minimum_width - 1),
            available.right(),
        )
        top = max(
            min(requested.top(), fixed_bottom - minimum_height + 1),
            available.top(),
        )
        return QRect(QPoint(fixed_left, top), QPoint(right, fixed_bottom))

    def _begin_move(self, global_pos: QPoint) -> None:
        self.raise_()
        self._move_origin = QPoint(global_pos)
        self._move_start_geometry = QRect(self.geometry())

    def _update_move(self, global_pos: QPoint) -> None:
        if self._move_origin is None or self._move_start_geometry is None:
            return
        requested = QRect(self._move_start_geometry)
        requested.moveTopLeft(
            self._move_start_geometry.topLeft() + global_pos - self._move_origin
        )
        self.setGeometry(self._effective_geometry(requested))
        self.raise_()

    def _finish_move(self) -> None:
        if self._move_origin is None or self._move_start_geometry is None:
            return
        start_geometry = self._move_start_geometry
        self._move_origin = None
        self._move_start_geometry = None
        if self.geometry() != start_geometry:
            self._commit_user_geometry(QRect(self.geometry()))

    def _begin_resize(self, global_pos: QPoint) -> None:
        self.raise_()
        self._resize_origin = QPoint(global_pos)
        self._resize_start_geometry = QRect(self.geometry())

    def _update_resize(self, global_pos: QPoint) -> None:
        if self._resize_origin is None or self._resize_start_geometry is None:
            return
        delta = global_pos - self._resize_origin
        requested = QRect(
            self._resize_start_geometry.left(),
            self._resize_start_geometry.top() + delta.y(),
            self._resize_start_geometry.width() + delta.x(),
            self._resize_start_geometry.height() - delta.y(),
        )
        self.setGeometry(self._effective_top_right_resize(requested))
        self.raise_()

    def _finish_resize(self) -> None:
        if self._resize_origin is None or self._resize_start_geometry is None:
            return
        start_geometry = self._resize_start_geometry
        self._resize_origin = None
        self._resize_start_geometry = None
        if self.geometry() != start_geometry:
            self._commit_user_geometry(QRect(self.geometry()))

    def sync_geometry(self) -> None:
        if self._move_origin is not None or self._resize_origin is not None:
            # Frame updates can queue page layout events between pointer events.
            # Keep the in-flight choice, applying only current parent bounds.
            effective = self._effective_geometry(
                self.geometry(), enforce_minimum=False
            )
        elif self._customized and self._requested_geometry is not None:
            effective = self._effective_geometry(self._requested_geometry)
        else:
            effective = self._effective_default_geometry()
        self.setGeometry(effective)
        self.setVisible(not effective.isEmpty())
        self.raise_()

    def reset_geometry(self) -> None:
        self._customized = False
        self._requested_geometry = None
        self.sync_geometry()
        self._persist_default_state()

    def _commit_user_geometry(self, rect: QRect) -> None:
        self._customized = True
        self._requested_geometry = QRect(rect)
        self.sync_geometry()
        self._save_custom_geometry()

    def _load_state(self) -> None:
        try:
            if not self._sync_settings():
                raise RuntimeError("settings sync failed")
            customized = self._settings.value(self.SETTINGS_CUSTOMIZED)
            geometry = self._settings.value(self.SETTINGS_GEOMETRY)
        except Exception:
            self._use_default_and_clear_invalid_state()
            return

        # INI backends reload serialized booleans as strings in a new process.
        if customized == "true":
            customized = True
        elif customized == "false":
            customized = False

        if customized is False:
            self._customized = False
            self._requested_geometry = None
            return

        if customized is True and self._is_valid_geometry(geometry):
            self._customized = True
            self._requested_geometry = QRect(geometry)
            return

        self._use_default_and_clear_invalid_state()

    @staticmethod
    def _is_valid_geometry(value) -> bool:
        return (
            isinstance(value, QRect)
            and not value.isEmpty()
            and value.width() > 0
            and value.height() > 0
        )

    def _sync_settings(self) -> bool:
        self._settings.sync()
        return self._settings.status() == QSettings.Status.NoError

    def _use_default_and_clear_invalid_state(self) -> None:
        self._customized = False
        self._requested_geometry = None
        group_started = False
        try:
            self._settings.beginGroup("ui/video_preview_overlay")
            group_started = True
            self._settings.remove("")
        except Exception:
            pass
        finally:
            if group_started:
                try:
                    self._settings.endGroup()
                except Exception:
                    pass
        try:
            self._sync_settings()
        except Exception:
            pass

    def _save_custom_geometry(self) -> bool:
        if not self._customized or self._requested_geometry is None:
            return False
        try:
            self._settings.setValue(
                self.SETTINGS_GEOMETRY, QRect(self._requested_geometry)
            )
            self._settings.setValue(self.SETTINGS_CUSTOMIZED, True)
            return self._sync_settings()
        except Exception:
            return False

    def _persist_default_state(self) -> bool:
        try:
            self._settings.setValue(self.SETTINGS_CUSTOMIZED, False)
            if not self._sync_settings():
                return False
        except Exception:
            return False

        try:
            self._settings.remove(self.SETTINGS_GEOMETRY)
            return self._sync_settings()
        except Exception:
            return False

    def _schedule_sync_geometry(self) -> None:
        if not self._sync_timer.isActive():
            self._sync_timer.start(0)

    def eventFilter(self, watched, event) -> bool:
        if watched in (self._page_ref(), self._anchor):
            if event.type() in (
                QEvent.Type.Show,
                QEvent.Type.Resize,
                QEvent.Type.Move,
                QEvent.Type.LayoutRequest,
            ):
                self._schedule_sync_geometry()
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resize_handle.move(max(0, self.width() - self.resize_handle.width()), 0)
        self.resize_handle.raise_()

    def _on_anchor_destroyed(self) -> None:
        self._anchor = None
        self.sync_geometry()

    def _on_page_destroyed(self) -> None:
        self._page_ref = lambda: None
