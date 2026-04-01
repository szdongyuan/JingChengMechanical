from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtWidgets import QFrame, QPushButton, QVBoxLayout, QWidget

from app.service.analysis.frames import StftFrame
from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.analysis_charts import StftImageChartWidget


@dataclass
class _StftCacheEntry:
    _INITIAL_COLUMN_CAPACITY: ClassVar[int] = 8

    frequencies_hz: list[float] = field(default_factory=list)
    times_sec: list[float] = field(default_factory=list)
    magnitude_db: np.ndarray | None = None
    _magnitude_storage: np.ndarray | None = field(default=None, init=False, repr=False)
    _column_capacity: int = field(default=0, init=False, repr=False)

    def append_frame(self, frame: StftFrame) -> bool:
        frequencies = np.asarray(getattr(frame, "frequencies", ()), dtype=float)
        magnitudes = np.asarray(getattr(frame, "magnitudes", ()), dtype=float)
        if frequencies.ndim != 1 or magnitudes.ndim != 1:
            return False
        if frequencies.shape != magnitudes.shape or frequencies.size == 0:
            return False

        if self.frequencies_hz:
            cached_frequencies = np.asarray(self.frequencies_hz, dtype=float)
            if frequencies.shape != cached_frequencies.shape:
                return False
            if not np.allclose(frequencies, cached_frequencies):
                return False

        if not self.frequencies_hz:
            self.frequencies_hz = frequencies.tolist()

        self._append_magnitude_column(magnitudes)
        self.times_sec.append(float(getattr(frame, "time", 0.0)))
        return True

    def has_data(self) -> bool:
        return bool(
            self.frequencies_hz
            and self.times_sec
            and self.magnitude_db is not None
            and self.magnitude_db.size > 0
        )

    def image_payload(self) -> dict | None:
        if not self.has_data():
            return None
        return {
            "times_sec": list(self.times_sec),
            "frequencies_hz": list(self.frequencies_hz),
            "magnitude_db": self.magnitude_db,
        }

    def _append_magnitude_column(self, magnitudes: np.ndarray) -> None:
        column_count = 0 if self.magnitude_db is None else self.magnitude_db.shape[1]
        self._ensure_magnitude_capacity(
            row_count=magnitudes.shape[0],
            required_columns=column_count + 1,
        )
        if self._magnitude_storage is None:
            raise RuntimeError("STFT cache storage was not initialized")
        self._magnitude_storage[:, column_count] = magnitudes
        self.magnitude_db = self._magnitude_storage[:, : column_count + 1]

    def _ensure_magnitude_capacity(self, *, row_count: int, required_columns: int) -> None:
        if (
            self._magnitude_storage is not None
            and self._magnitude_storage.shape[0] == row_count
            and self._column_capacity >= required_columns
        ):
            return

        existing_matrix = self.magnitude_db
        existing_columns = 0 if existing_matrix is None else existing_matrix.shape[1]
        new_capacity = self._next_column_capacity(required_columns)
        new_storage = np.empty((row_count, new_capacity), dtype=float)
        if existing_matrix is not None and existing_columns > 0:
            new_storage[:, :existing_columns] = existing_matrix
        self._magnitude_storage = new_storage
        self._column_capacity = new_capacity
        if existing_columns > 0:
            self.magnitude_db = self._magnitude_storage[:, :existing_columns]

    def _next_column_capacity(self, required_columns: int) -> int:
        capacity = max(self._column_capacity, self._INITIAL_COLUMN_CAPACITY)
        while capacity < required_columns:
            capacity *= 2
        return capacity


@dataclass(frozen=True)
class _OverlayGeometry:
    heatmap_rect: QRect
    plot_rect: QRect
    rendered_start_sec: float
    rendered_end_sec: float


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
            left_px = (
                float(event.globalPosition().x())
                - canvas_origin.x()
                - self._owner._drag_offset_px
            )
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
        self.heatmap = StftImageChartWidget(self)

    def resizeEvent(self, event):
        self.heatmap.setGeometry(self.rect())
        self._owner._update_overlay_geometry()
        super().resizeEvent(event)


class StftModule(QWidget):
    module_id = "stft"
    _DISPLAY_PHASES = frozenset({"running", "analyzing", "reviewing", "reanalyzing"})
    _REVIEW_PHASES = frozenset({"reviewing", "reanalyzing"})
    _LIVE_FLUSH_INTERVAL_MS = 50

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.selected_display_channel_provider = None
        self._factory_state = getattr(
            services.factory_mode, "state", FactoryControllerState()
        )
        self._review_payload = getattr(
            services.factory_mode, "review_payload", FactoryReviewPayload()
        )
        self._selected_window: tuple[float, float] | None = None
        self._drag_offset_px: float | None = None
        self._display_run_id = getattr(self._review_payload, "run_id", None)
        self._render_cache_key: tuple[int, str, int] | None = None
        self._stft_cache: dict[tuple[int, str, int], _StftCacheEntry] = {}
        self._pending_live_flush_key: tuple[int, str, int] | None = None
        self._live_flush_generation = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.canvas = _HeatmapCanvas(self)
        lay.addWidget(self.canvas, 1)

        self.selection_box = _AnalysisSelectionBox(self)
        self.analyze_button = QPushButton("分析", self.canvas)
        self.analyze_button.clicked.connect(self._begin_reanalysis)
        self.analyze_button.hide()

        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(
                self._on_factory_state_changed
            )
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(
                self._on_factory_review_changed
            )
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(self.services.bus, "factory_runtime_start_accepted"):
            self.services.bus.factory_runtime_start_accepted.connect(
                self._on_factory_runtime_context
            )
        if hasattr(self.services.bus, "factory_runtime_analyzing_entered"):
            self.services.bus.factory_runtime_analyzing_entered.connect(
                self._on_factory_runtime_context
            )
        if hasattr(self.services.bus, "stft_updated"):
            self.services.bus.stft_updated.connect(self._on_stft_updated)

        self._on_factory_review_changed(self._review_payload)
        self._on_factory_state_changed(self._factory_state)

    @property
    def selected_window(self) -> tuple[float, float] | None:
        return self._selected_window

    def has_cached_run_data(
        self,
        run_id: str,
        *,
        channel_index: int,
        config_id: int | None = None,
    ) -> bool:
        for cache_key, cache_entry in self._stft_cache.items():
            cache_config_id, cache_run_id, cache_channel_index = cache_key
            if config_id is not None and cache_config_id != config_id:
                continue
            if cache_run_id == run_id and cache_channel_index == channel_index:
                return cache_entry.has_data()
        return False

    def current_render_cache_key(self) -> tuple[int, str, int] | None:
        return self._render_cache_key

    def current_image_payload(self) -> dict | None:
        if self._render_cache_key is not None:
            cache_entry = self._stft_cache.get(self._render_cache_key)
            payload = None if cache_entry is None else cache_entry.image_payload()
            if payload is not None:
                return payload
        return self.canvas.heatmap.image_payload()

    def pending_live_flush_key(self) -> tuple[int, str, int] | None:
        return self._pending_live_flush_key

    def refresh_selected_display_channel(self) -> None:
        self._refresh_cached_rendering()
        self._update_overlay_geometry()

    def flush_pending_live_refresh(self) -> None:
        pending_cache_key = self._pending_live_flush_key
        if pending_cache_key is None:
            return

        self._clear_pending_live_flush()
        resolved_render_target = self._resolve_render_target()
        if resolved_render_target is None:
            return

        cache_key, payload = resolved_render_target
        if cache_key != pending_cache_key:
            return

        self._render_payload(cache_key, payload)
        self._update_overlay_geometry()

    def move_selection_to_start(self, start_sec: float) -> bool:
        if not self.is_selection_movable() or self._selected_window is None:
            return False

        geometry = self._current_overlay_geometry()
        if geometry is None:
            return False

        return self._move_selection_to_start(start_sec, geometry=geometry)

    def move_selection_to_track_x(self, left_px: float) -> bool:
        geometry = self._current_overlay_geometry()
        if self._selected_window is None or geometry is None:
            return False

        plot_rect = geometry.plot_rect
        duration = self._selected_window[1] - self._selected_window[0]
        start_bounds = self._selection_start_bounds(duration, geometry=geometry)
        if start_bounds is None:
            return False
        min_start_sec, max_start_sec = start_bounds

        requested_left_px = float(left_px)
        plot_left = float(plot_rect.left())
        plot_right = float(plot_rect.right())
        if requested_left_px <= plot_left:
            start_sec = min_start_sec
        elif requested_left_px >= plot_right and max_start_sec is not None:
            start_sec = max_start_sec
        else:
            clamped_left_px = max(plot_left, min(requested_left_px, plot_right))
            start_sec = self.canvas.heatmap.time_sec_for_widget_x(
                clamped_left_px - float(geometry.heatmap_rect.left())
            )
            if start_sec is None:
                return False
        return self._move_selection_to_start(start_sec, geometry=geometry)

    def is_selection_movable(self) -> bool:
        return (
            self._selected_window is not None
            and bool(getattr(self._review_payload, "manual_reanalysis_allowed", False))
            and self._factory_state.run_phase == "reviewing"
        )

    def _on_factory_state_changed(self, state):
        previous_config_id = self._factory_state.displayed_config_id
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()

        if not self._has_display_context():
            self._display_run_id = None
        elif self._factory_state.displayed_config_id != previous_config_id:
            self._display_run_id = self._review_run_id_for_displayed_config()

        self._refresh_cached_rendering()
        self._update_overlay_geometry()

    def _on_factory_review_changed(self, review):
        if isinstance(review, FactoryReviewPayload):
            if self._should_preserve_selection(review):
                self._review_payload = review
                self._sync_display_run_id_from_review(review)
                self._refresh_cached_rendering()
                self._update_overlay_geometry()
                return
            self._review_payload = review
        else:
            self._review_payload = FactoryReviewPayload()

        self._selected_window = self._selection_from_review(self._review_payload)
        self._sync_display_run_id_from_review(self._review_payload)
        self._refresh_cached_rendering()
        self._update_overlay_geometry()

    def _on_factory_display_reset(self, _reason: str):
        self._review_payload = FactoryReviewPayload()
        self._selected_window = None
        self._drag_offset_px = None
        self._display_run_id = None
        self._render_cache_key = None
        self._clear_pending_live_flush()
        self._stft_cache.clear()
        self.canvas.heatmap.clear_data()
        self._update_overlay_geometry()

    def _on_factory_runtime_context(self, payload) -> None:
        config_id = getattr(payload, "config_id", None)
        run_id = getattr(payload, "run_id", None)
        if (
            config_id == self._factory_state.displayed_config_id
            and isinstance(run_id, str)
            and run_id
        ):
            self._display_run_id = run_id
            self._refresh_cached_rendering()

    def _on_stft_updated(
        self,
        config_id: int,
        run_id: str,
        channel_idx: int,
        frame,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            return
        try:
            normalized_config_id = int(config_id)
            normalized_channel_idx = int(channel_idx)
        except (TypeError, ValueError):
            return

        cache_entry = self._stft_cache.setdefault(
            (normalized_config_id, run_id, normalized_channel_idx),
            _StftCacheEntry(),
        )
        if not cache_entry.append_frame(frame):
            return

        if (
            not self._has_display_context()
            or normalized_config_id != self._factory_state.displayed_config_id
        ):
            return

        if self._display_run_id is None:
            self._display_run_id = run_id
        if run_id != self._display_run_id:
            return

        cache_key = (normalized_config_id, run_id, normalized_channel_idx)
        render_channel = self._resolve_render_channel(normalized_config_id, run_id)
        if render_channel != normalized_channel_idx:
            return

        if self._factory_state.run_phase in self._REVIEW_PHASES:
            self._refresh_cached_rendering()
            self._update_overlay_geometry()
            return

        if (
            self._factory_state.run_phase in {"running", "analyzing"}
            and self._render_cache_key == cache_key
            and self.canvas.heatmap.has_image()
        ):
            self._schedule_live_refresh(cache_key)
            return

        self._refresh_cached_rendering()
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

        geometry = self._current_overlay_geometry()
        if geometry is None:
            self.selection_box.hide()
            self.analyze_button.hide()
            return

        normalized_window = self._normalized_selection_window(
            self._selected_window,
            geometry=geometry,
        )
        if normalized_window is None:
            self.selection_box.hide()
            self.analyze_button.hide()
            return
        self._selected_window = normalized_window

        plot_rect = geometry.plot_rect
        heatmap_left = float(geometry.heatmap_rect.left())
        start_sec, end_sec = normalized_window
        left_px = self.canvas.heatmap.widget_x_for_time_sec(start_sec)
        right_px = self.canvas.heatmap.widget_x_for_time_sec(end_sec)
        if left_px is None or right_px is None:
            self.selection_box.hide()
            self.analyze_button.hide()
            return

        left = int(
            round(
                max(
                    float(plot_rect.left()),
                    min(heatmap_left + left_px, float(plot_rect.right())),
                )
            )
        )
        right = int(
            round(
                max(
                    float(plot_rect.left()),
                    min(heatmap_left + right_px, float(plot_rect.right())),
                )
            )
        )
        if right < left:
            right = left
        width = max(1, right - left + 1)
        self.selection_box.setGeometry(left, plot_rect.top(), width, plot_rect.height())

        if not button_visible:
            return

        button_size = self.analyze_button.sizeHint()
        button_width = max(button_size.width() + 6, 58)
        button_height = button_size.height()
        button_x = left + width + 8
        if button_x + button_width > plot_rect.right() + 1:
            button_x = max(plot_rect.left(), left - button_width - 8)
        button_y = max(
            plot_rect.top(),
            min(plot_rect.top() + 8, plot_rect.bottom() - button_height + 1),
        )
        self.analyze_button.setGeometry(button_x, button_y, button_width, button_height)

    def _update_selection_cursor(self) -> None:
        if self.is_selection_movable():
            self.selection_box.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.selection_box.setCursor(Qt.CursorShape.ArrowCursor)

    def _current_overlay_geometry(self) -> _OverlayGeometry | None:
        heatmap_rect = self.canvas.heatmap.geometry()
        plot_rect = self.canvas.heatmap.plot_area_rect_in_widget()
        if plot_rect is None or plot_rect.width() <= 0 or plot_rect.height() <= 0:
            return None

        rendered_time_extent_sec = self.canvas.heatmap.rendered_time_extent_sec()
        if rendered_time_extent_sec is None:
            return None

        rendered_start_sec, rendered_end_sec = rendered_time_extent_sec
        if rendered_end_sec <= rendered_start_sec:
            return None

        return _OverlayGeometry(
            heatmap_rect=QRect(heatmap_rect),
            plot_rect=QRect(plot_rect).translated(heatmap_rect.topLeft()),
            rendered_start_sec=float(rendered_start_sec),
            rendered_end_sec=float(rendered_end_sec),
        )

    def _move_selection_to_start(
        self,
        start_sec: float,
        *,
        geometry: _OverlayGeometry | None,
    ) -> bool:
        if not self.is_selection_movable() or self._selected_window is None:
            return False

        duration = self._selected_window[1] - self._selected_window[0]
        normalized_window = self._normalized_selection_window(
            (float(start_sec), float(start_sec) + duration),
            geometry=geometry,
        )
        if normalized_window is None:
            return False

        self._selected_window = normalized_window
        self._update_overlay_geometry()
        return True

    def _normalized_selection_window(
        self,
        window: tuple[float, float] | None,
        *,
        geometry: _OverlayGeometry | None,
    ) -> tuple[float, float] | None:
        if window is None:
            return None

        try:
            start_sec = float(window[0])
            end_sec = float(window[1])
        except (TypeError, ValueError):
            return None

        duration = end_sec - start_sec
        if duration <= 0:
            return None

        start_bounds = self._selection_start_bounds(duration, geometry=geometry)
        if start_bounds is None:
            return None

        min_start_sec, max_start_sec = start_bounds
        clamped_start_sec = max(min_start_sec, start_sec)
        if max_start_sec is not None:
            clamped_start_sec = min(clamped_start_sec, max_start_sec)
        return (clamped_start_sec, clamped_start_sec + duration)

    def _selection_start_bounds(
        self,
        duration_sec: float,
        *,
        geometry: _OverlayGeometry | None,
    ) -> tuple[float, float | None] | None:
        if duration_sec <= 0:
            return None

        min_start_sec = 0.0
        max_end_sec: float | None = None

        captured_duration_sec = self._captured_duration_sec()
        if captured_duration_sec is not None:
            if captured_duration_sec < duration_sec:
                return None
            max_end_sec = captured_duration_sec

        if geometry is not None:
            min_start_sec = max(min_start_sec, geometry.rendered_start_sec)
            max_end_sec = (
                geometry.rendered_end_sec
                if max_end_sec is None
                else min(max_end_sec, geometry.rendered_end_sec)
            )

        if max_end_sec is None:
            return (min_start_sec, None)

        max_start_sec = max_end_sec - duration_sec
        if max_start_sec < min_start_sec:
            return None
        return (min_start_sec, max_start_sec)

    def _has_display_context(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._DISPLAY_PHASES
        )

    def _review_run_id_for_displayed_config(self) -> str | None:
        if (
            self._review_payload.displayed_config_id == self._factory_state.displayed_config_id
            and isinstance(self._review_payload.run_id, str)
            and self._review_payload.run_id
        ):
            return self._review_payload.run_id
        return None

    def _sync_display_run_id_from_review(self, review: FactoryReviewPayload) -> None:
        if (
            review.displayed_config_id == self._factory_state.displayed_config_id
            and isinstance(review.run_id, str)
            and review.run_id
        ):
            self._display_run_id = review.run_id
        elif self._factory_state.run_phase in self._REVIEW_PHASES:
            self._display_run_id = None

    def _refresh_cached_rendering(self, *, boundary_render: bool = True) -> None:
        if boundary_render:
            self._clear_pending_live_flush()

        resolved_render_target = self._resolve_render_target()
        if resolved_render_target is None:
            self._clear_rendering()
            return

        cache_key, payload = resolved_render_target
        self._render_payload(cache_key, payload)

    def _resolve_render_target(self) -> tuple[tuple[int, str, int], dict] | None:
        if not self._has_display_context():
            return None

        config_id = self._factory_state.displayed_config_id
        run_id = self._display_run_id or self._review_run_id_for_displayed_config()
        if config_id is None or run_id is None:
            return None

        channel_index = self._resolve_render_channel(config_id, run_id)
        if channel_index is None:
            return None

        cache_key = (config_id, run_id, channel_index)
        cache_entry = self._stft_cache.get(cache_key)
        payload = None if cache_entry is None else cache_entry.image_payload()
        if payload is None:
            return None

        return (cache_key, payload)

    def _render_payload(self, cache_key: tuple[int, str, int], payload: dict) -> None:
        reset_levels = cache_key != self._render_cache_key or not self.canvas.heatmap.has_image()
        self._render_cache_key = cache_key
        self.canvas.heatmap.set_image(
            times_sec=payload["times_sec"],
            frequencies_hz=payload["frequencies_hz"],
            magnitude_db=payload["magnitude_db"],
            reset_levels=reset_levels,
        )

    def _clear_rendering(self) -> None:
        self._render_cache_key = None
        self.canvas.heatmap.clear_data()

    def _schedule_live_refresh(self, cache_key: tuple[int, str, int]) -> None:
        if self._pending_live_flush_key == cache_key:
            return

        self._pending_live_flush_key = cache_key
        self._live_flush_generation += 1
        generation = self._live_flush_generation
        self._schedule_live_flush_callback(
            lambda generation=generation: self._flush_pending_live_refresh_if_current(
                generation
            )
        )

    def _schedule_live_flush_callback(self, callback) -> None:
        QTimer.singleShot(self._LIVE_FLUSH_INTERVAL_MS, callback)

    def _flush_pending_live_refresh_if_current(self, generation: int) -> None:
        if generation != self._live_flush_generation:
            return
        self.flush_pending_live_refresh()

    def _clear_pending_live_flush(self) -> None:
        if self._pending_live_flush_key is None:
            return
        self._pending_live_flush_key = None
        self._live_flush_generation += 1

    def _resolve_render_channel(self, config_id: int, run_id: str) -> int | None:
        selected_channel = None
        provider = self.selected_display_channel_provider
        if callable(provider):
            provided_channel = provider()
            if isinstance(provided_channel, int):
                selected_channel = provided_channel

        if selected_channel is not None:
            return selected_channel

        available_channels = self._available_cached_channels(config_id, run_id)
        if available_channels:
            return available_channels[0]
        return None

    def _available_cached_channels(self, config_id: int, run_id: str) -> list[int]:
        channels: list[int] = []
        for cache_key, cache_entry in self._stft_cache.items():
            cache_config_id, cache_run_id, channel_index = cache_key
            if (
                cache_config_id == config_id
                and cache_run_id == run_id
                and cache_entry.has_data()
                and channel_index not in channels
            ):
                channels.append(channel_index)
        return channels
