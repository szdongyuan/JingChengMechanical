from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtWidgets import QFrame, QPushButton, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.fake_plots import FakeHeatmap


class _AnalysisSelectionBox(QFrame):
    def __init__(self, owner: "StftModule"):
        super().__init__(owner.canvas)
        self._owner = owner
        self.setObjectName("StftAnalysisSelectionBox")
        self.setStyleSheet(
            "#StftAnalysisSelectionBox {"
            "background-color: rgba(59, 130, 246, 48);"
            "border: 2px solid #3b82f6;"
            "border-radius: 6px;"
            "}"
        )
        self.hide()

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._owner.is_selection_movable()
        ):
            canvas_origin = self._owner.canvas.mapToGlobal(QPoint(0, 0))
            self._owner._drag_offset_px = float(event.globalPosition().x()) - (
                canvas_origin.x() + self.x()
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._owner._drag_offset_px is not None:
            canvas_origin = self._owner.canvas.mapToGlobal(QPoint(0, 0))
            left_px = float(event.globalPosition().x()) - canvas_origin.x() - self._owner._drag_offset_px
            self._owner.move_selection_to_track_x(left_px)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._owner._drag_offset_px is not None:
            self._owner._drag_offset_px = None
            self._owner._update_selection_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _HeatmapCanvas(QWidget):
    def __init__(self, owner: "StftModule"):
        super().__init__(owner)
        self._owner = owner
        self.heatmap = FakeHeatmap(self)

    def resizeEvent(self, event):
        self.heatmap.setGeometry(self.rect())
        self._owner._update_overlay_geometry()
        super().resizeEvent(event)


class StftModule(QWidget):
    module_id = "stft"
    _REVIEW_PHASES = frozenset({"reviewing", "reanalyzing"})
    _SELECTION_MARGIN_PX = 12

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._review_payload = getattr(services.factory_mode, "review_payload", FactoryReviewPayload())
        self._selected_window: tuple[float, float] | None = None
        self._drag_offset_px: float | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.canvas = _HeatmapCanvas(self)
        lay.addWidget(self.canvas, 1)

        self.selection_box = _AnalysisSelectionBox(self)
        self.analyze_button = QPushButton("分析", self.canvas)
        self.analyze_button.clicked.connect(self._begin_reanalysis)
        self.analyze_button.hide()

        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)

        self._on_factory_review_changed(self._review_payload)
        self._on_factory_state_changed(self._factory_state)

    @property
    def selected_window(self) -> tuple[float, float] | None:
        return self._selected_window

    def move_selection_to_start(self, start_sec: float) -> bool:
        if not self.is_selection_movable() or self._selected_window is None:
            return False

        duration = self._selected_window[1] - self._selected_window[0]
        captured_duration_sec = self._captured_duration_sec()
        if duration <= 0 or captured_duration_sec is None or captured_duration_sec < duration:
            return False

        clamped_start = max(0.0, min(float(start_sec), captured_duration_sec - duration))
        self._selected_window = (clamped_start, clamped_start + duration)
        self._update_overlay_geometry()
        return True

    def move_selection_to_track_x(self, left_px: float) -> bool:
        track = self._track_rect()
        captured_duration_sec = self._captured_duration_sec()
        if (
            self._selected_window is None
            or track.width() <= 0
            or captured_duration_sec is None
            or captured_duration_sec <= 0
        ):
            return False

        left_px = max(float(track.left()), min(float(left_px), float(track.right())))
        start_ratio = (left_px - float(track.left())) / float(track.width())
        return self.move_selection_to_start(start_ratio * captured_duration_sec)

    def is_selection_movable(self) -> bool:
        return (
            self._selected_window is not None
            and bool(getattr(self._review_payload, "manual_reanalysis_allowed", False))
            and self._factory_state.run_phase == "reviewing"
        )

    def _on_factory_state_changed(self, state):
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
            self._update_overlay_geometry()

    def _on_factory_review_changed(self, review):
        if isinstance(review, FactoryReviewPayload):
            if self._should_preserve_selection(review):
                self._review_payload = review
                self._update_overlay_geometry()
                return
            self._review_payload = review
        else:
            self._review_payload = FactoryReviewPayload()

        self._selected_window = self._selection_from_review(self._review_payload)
        self._update_overlay_geometry()

    def _on_factory_display_reset(self, _reason: str):
        self._review_payload = FactoryReviewPayload()
        self._selected_window = None
        self._drag_offset_px = None
        self._update_overlay_geometry()

    def _begin_reanalysis(self):
        if self._selected_window is None or not self.is_selection_movable():
            return

        validated = self._validated_window(
            start_sec=self._selected_window[0],
            end_sec=self._selected_window[1],
            captured_duration_sec=self._captured_duration_sec(),
        )
        if validated is None:
            return

        factory_mode = getattr(self.services, "factory_mode", None)
        begin_reanalysis = getattr(factory_mode, "begin_reanalysis", None)
        if callable(begin_reanalysis):
            begin_reanalysis(validated[0], validated[1])

    def _selection_from_review(
        self,
        review: FactoryReviewPayload,
    ) -> tuple[float, float] | None:
        if review.displayed_config_id is None:
            return None

        if review.no_result or review.error_message:
            if review.manual_reanalysis_allowed:
                return self._first_valid_window(review)
            return self._window_from_payload_window(review.default_window)

        return self._window_from_payload_window(review.default_window)

    def _first_valid_window(self, review: FactoryReviewPayload) -> tuple[float, float] | None:
        fallback = self._window_from_payload_window(review.fallback_window)
        if fallback is not None:
            return fallback

        duration = self._window_duration(review.default_window)
        captured_duration_sec = self._captured_duration_sec(review)
        if (
            duration is None
            or captured_duration_sec is None
            or captured_duration_sec < duration
        ):
            return None
        return (0.0, duration)

    def _window_from_payload_window(self, review_window) -> tuple[float, float] | None:
        if review_window is None:
            return None
        return self._validated_window(
            start_sec=review_window.start_sec,
            end_sec=review_window.end_sec,
            captured_duration_sec=self._captured_duration_sec(),
        )

    def _validated_window(
        self,
        *,
        start_sec: float,
        end_sec: float,
        captured_duration_sec: float | None,
    ) -> tuple[float, float] | None:
        try:
            start = float(start_sec)
            end = float(end_sec)
        except (TypeError, ValueError):
            return None

        duration = end - start
        if duration <= 0:
            return None

        if captured_duration_sec is None:
            return (start, end) if start >= 0 else None

        if captured_duration_sec < duration:
            return None

        clamped_start = max(0.0, min(start, captured_duration_sec - duration))
        return (clamped_start, clamped_start + duration)

    def _captured_duration_sec(self, review: FactoryReviewPayload | None = None) -> float | None:
        review = review or self._review_payload
        try:
            captured_duration_sec = float(review.captured_duration_sec)
        except (TypeError, ValueError):
            return None
        return captured_duration_sec if captured_duration_sec > 0 else None

    @staticmethod
    def _window_duration(review_window) -> float | None:
        if review_window is None:
            return None
        try:
            duration = float(review_window.end_sec) - float(review_window.start_sec)
        except (TypeError, ValueError):
            return None
        return duration if duration > 0 else None

    def _should_preserve_selection(self, review: FactoryReviewPayload) -> bool:
        if self._selected_window is None:
            return False
        if self._factory_state.run_phase == "reanalyzing":
            return not self._is_reanalysis_success_review(review)
        return (
            review.displayed_config_id == self._review_payload.displayed_config_id
            and review.run_id == self._review_payload.run_id
            and review.judgment_payload is None
            and not review.no_result
            and not review.error_message
        )

    def _is_reanalysis_success_review(self, review: FactoryReviewPayload) -> bool:
        return (
            review.displayed_config_id == self._review_payload.displayed_config_id
            and review.run_id == self._review_payload.run_id
            and review.judgment_payload is not None
            and not review.no_result
            and not review.error_message
        )

    def _update_overlay_geometry(self) -> None:
        in_review_phase = self._factory_state.run_phase in self._REVIEW_PHASES
        has_selection = self._selected_window is not None and in_review_phase
        self.selection_box.setVisible(has_selection)

        button_visible = has_selection and bool(self._review_payload.manual_reanalysis_allowed)
        self.analyze_button.setVisible(button_visible)
        self.analyze_button.setEnabled(button_visible and self._factory_state.run_phase == "reviewing")

        self._update_selection_cursor()
        if not has_selection:
            return

        track = self._track_rect()
        captured_duration_sec = self._captured_duration_sec()
        if track.width() <= 0 or track.height() <= 0 or captured_duration_sec is None:
            self.selection_box.hide()
            self.analyze_button.hide()
            return

        start_sec, end_sec = self._selected_window
        duration = end_sec - start_sec
        if duration <= 0 or captured_duration_sec < duration:
            self.selection_box.hide()
            self.analyze_button.hide()
            return

        left = int(round(track.left() + (start_sec / captured_duration_sec) * track.width()))
        width = max(1, int(round((duration / captured_duration_sec) * track.width())))
        max_left = track.right() - width + 1
        left = max(track.left(), min(left, max_left))
        self.selection_box.setGeometry(left, track.top(), width, track.height())

        if not button_visible:
            return

        button_size = self.analyze_button.sizeHint()
        button_width = max(button_size.width() + 6, 58)
        button_height = button_size.height()
        button_x = left + width + 8
        if button_x + button_width > track.right() + 1:
            button_x = max(track.left(), left - button_width - 8)
        button_y = max(track.top(), min(track.top() + 8, track.bottom() - button_height + 1))
        self.analyze_button.setGeometry(button_x, button_y, button_width, button_height)

    def _update_selection_cursor(self) -> None:
        if self.is_selection_movable():
            self.selection_box.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.selection_box.setCursor(Qt.CursorShape.ArrowCursor)

    def _track_rect(self) -> QRect:
        return self.canvas.rect().adjusted(
            self._SELECTION_MARGIN_PX,
            self._SELECTION_MARGIN_PX,
            -self._SELECTION_MARGIN_PX,
            -self._SELECTION_MARGIN_PX,
        )
