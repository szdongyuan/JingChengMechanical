from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.service.analysis.frames import StftFrame
from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.analysis_charts import StftImageChartWidget


@dataclass
class _StftCacheEntry:
    _INITIAL_COLUMN_CAPACITY: ClassVar[int] = 8

    frequencies_hz: list[float] = field(default_factory=list)
    times_sec: list[float] = field(default_factory=list)
    magnitude_db: np.ndarray | None = None
    snap_initial_to_zero: bool = True
    render_revision: int = 0
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
        self.render_revision += 1
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
    _ACTIVE_STYLE = (
        "#StftAnalysisSelectionBox {"
        "background-color: rgba(59, 130, 246, 48);"
        "border: 2px solid #3b82f6;"
        "border-radius: 6px;"
        "}"
    )
    _PASSIVE_STYLE = (
        "#StftAnalysisSelectionBox {"
        "background-color: rgba(59, 130, 246, 16);"
        "border: 1px dashed rgba(59, 130, 246, 110);"
        "border-radius: 4px;"
        "}"
    )

    def __init__(self, owner: "StftModule"):
        super().__init__(owner.canvas)
        self._owner = owner
        self._left_press_global_pos: QPoint | None = None
        self._left_press_inside_plot = False
        self._left_drag_exceeded = False
        self.setObjectName("StftAnalysisSelectionBox")
        self.setMouseTracking(True)
        self.set_selection_mode_active(False)
        self.hide()

    def set_selection_mode_active(self, active: bool) -> None:
        self.setStyleSheet(self._ACTIVE_STYLE if active else self._PASSIVE_STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not active)

    def mousePressEvent(self, event):
        if self._owner._analysis_selection_mode:
            if event.button() == Qt.MouseButton.LeftButton:
                self._owner._begin_analysis_selection_from_selection_box_event(event)
                event.accept()
                return
            if event.button() == Qt.MouseButton.RightButton:
                self._owner._clear_score_inspection_state()
                event.accept()
                return

        if event.button() == Qt.MouseButton.RightButton:
            self._owner._clear_score_inspection_state()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self._left_press_global_pos = event.globalPosition().toPoint()
            self._left_press_inside_plot = self._event_time_sec(event) is not None
            self._left_drag_exceeded = False
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._owner._analysis_selection_mode:
            self._owner._update_analysis_selection_from_selection_box_event(event)
            event.accept()
            return

        self._update_left_drag_state(event)

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

        if self._left_press_global_pos is not None and self._left_drag_exceeded:
            event.accept()
            return

        time_sec = self._event_time_sec(event)
        if time_sec is None:
            self._owner._clear_hover_score()
        else:
            self._owner._show_hover_score_for_time(time_sec)
        event.accept()
        return

    def mouseReleaseEvent(self, event):
        if self._owner._analysis_selection_mode:
            if event.button() == Qt.MouseButton.LeftButton:
                self._owner._finish_analysis_selection_from_selection_box_event(event)
                event.accept()
                return

        self._update_left_drag_state(event)
        was_dragging_selection = self._owner._drag_offset_px is not None
        if self._owner._drag_offset_px is not None:
            self._owner._drag_offset_px = None
            self._owner._update_selection_cursor()

        should_pin = (
            event.button() == Qt.MouseButton.LeftButton
            and self._left_press_global_pos is not None
            and self._left_press_inside_plot
            and not self._left_drag_exceeded
        )
        if should_pin:
            time_sec = self._event_time_sec(event)
            if time_sec is not None:
                self._owner._pin_score_for_time(time_sec)

        if was_dragging_selection or should_pin or self._left_drag_exceeded:
            self._reset_left_press_tracking()
            event.accept()
            return

        self._reset_left_press_tracking()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if self._owner._drag_offset_px is None:
            self._owner._clear_hover_score()
        super().leaveEvent(event)

    def _event_time_sec(self, event) -> float | None:
        position = self._event_position(event)
        if position is None:
            return None
        return self._owner._time_sec_for_selection_box_pos(position)

    @staticmethod
    def _event_position(event) -> QPoint | None:
        position = getattr(event, "position", None)
        if callable(position):
            return position().toPoint()

        position = getattr(event, "pos", None)
        if callable(position):
            return position()

        return None

    def _reset_left_press_tracking(self) -> None:
        self._left_press_global_pos = None
        self._left_press_inside_plot = False
        self._left_drag_exceeded = False

    def _update_left_drag_state(self, event) -> None:
        if self._left_press_global_pos is None:
            return

        movement_px = (
            event.globalPosition().toPoint() - self._left_press_global_pos
        ).manhattanLength()
        if movement_px > QApplication.startDragDistance():
            self._left_drag_exceeded = True


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
    score_title_changed = pyqtSignal(str)
    _DISPLAY_PHASES = frozenset(
        {"running", "analyzing", "reviewing", "reanalyzing", "stopped"}
    )
    _REVIEW_PHASES = frozenset({"reviewing", "reanalyzing"})
    _LIVE_FLUSH_INTERVAL_MS = 50
    _SCORE_TITLE_PLACEHOLDER = "得分：--"
    _MIN_MANUAL_SELECTION_DURATION_SEC = 0.1

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
        self._analysis_selection_mode = False
        self._analysis_drag_start_sec: float | None = None
        self._display_run_id = getattr(self._review_payload, "run_id", None)
        self._display_run_id_locked_by_runtime = False
        self._score_payload_by_channel: dict[int, dict] = {}
        self._current_trace_payload_by_channel: dict[int, dict] = {}
        self._live_current_trace_payload_by_run: dict[tuple[int, str], dict] = {}
        self._current_score_text = self._SCORE_TITLE_PLACEHOLDER
        self._score_times_sec: list[float] = []
        self._score_values: list[float] = []
        self._score_series_channel: int | None = None
        self._vibration_mode = False
        self._pinned_score_index: int | None = None
        self._hover_score_index: int | None = None
        self._render_cache_key: tuple[int, str, int] | None = None
        self._last_render_signature: (
            tuple[tuple[int, str, int], int, tuple[float, float] | None, bool] | None
        ) = None
        self._stft_cache: dict[tuple[int, str, int], _StftCacheEntry] = {}
        self._pending_live_flush_key: tuple[int, str, int] | None = None
        self._live_flush_generation = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.canvas = _HeatmapCanvas(self)
        lay.addWidget(self.canvas, 1)

        self.selection_box = _AnalysisSelectionBox(self)
        self.analysis_select_button = QPushButton("框选分析", self)
        self.analysis_select_button.setCheckable(True)
        self.analysis_select_button.setStyleSheet("padding: 2px 14px;")
        self.analysis_select_button.clicked.connect(self._on_analysis_select_toggled)
        self.analysis_select_button.hide()
        self.analyze_button = QPushButton("分析", self.canvas)
        self.analyze_button.clicked.connect(self._begin_reanalysis)
        self.analyze_button.hide()
        self.replay_button = QPushButton("重放", self.canvas)
        self.replay_button.clicked.connect(self._replay_selection)
        self.replay_button.hide()

        self.score_overlay_label = QLabel(self.canvas)
        self.score_overlay_label.setObjectName("StftScoreOverlayLabel")
        self.score_overlay_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.score_overlay_label.setStyleSheet(
            "#StftScoreOverlayLabel {"
            "background-color: rgba(15, 23, 42, 210);"
            "color: white;"
            "border-radius: 4px;"
            "padding: 4px 8px;"
            "font-weight: 600;"
            "}"
        )
        self.score_overlay_label.hide()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.canvas.heatmap.hover_time_changed.connect(self._show_hover_score_for_time)
        self.canvas.heatmap.left_time_clicked.connect(self._pin_score_for_time)
        self.canvas.heatmap.right_clicked.connect(self._clear_score_inspection_state)
        self.canvas.heatmap.mouse_left_plot.connect(self._clear_hover_score)
        self.canvas.heatmap.plot_widget.viewport().installEventFilter(self)
        self.canvas.heatmap.plot_item.vb.sigRangeChanged.connect(
            self._on_heatmap_view_range_changed
        )

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
        if hasattr(self.services.bus, "current_trace_updated"):
            self.services.bus.current_trace_updated.connect(
                self._on_current_trace_updated
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

    def set_vibration_mode(self, enabled: bool) -> None:
        self._vibration_mode = bool(enabled)
        self.canvas.heatmap.plot_item.setLabel("bottom", "时间", units="s")
        self.canvas.heatmap.plot_item.setLabel("left", "频率", units="Hz")
        if getattr(self.canvas.heatmap, "_hide_left_axis_label", False):
            self.canvas.heatmap.apply_left_axis_label_hidden_style()

    def current_score_title(self) -> str:
        return self._current_score_text

    def hover_score_text(self) -> str | None:
        return self._score_text_for_index(self._hover_score_index)

    def pinned_score_text(self) -> str | None:
        return self._score_text_for_index(self._pinned_score_index)

    def _time_sec_for_selection_box_pos(
        self,
        position: QPoint,
        *,
        clamp_to_plot: bool = False,
    ) -> float | None:
        heatmap_rect = self.canvas.heatmap.geometry()
        heatmap_pos = self.selection_box.mapTo(self.canvas, position)
        heatmap_pos -= heatmap_rect.topLeft()

        plot_rect = self.canvas.heatmap.plot_area_rect_in_widget()
        if plot_rect is None:
            return None
        if clamp_to_plot:
            x = max(plot_rect.left(), min(heatmap_pos.x(), plot_rect.right()))
        else:
            if not plot_rect.contains(heatmap_pos):
                return None
            x = heatmap_pos.x()

        return self.canvas.heatmap.time_sec_for_widget_x(float(x))

    def refresh_selected_display_channel(self) -> None:
        selected_channel = self._selected_display_channel()
        channel_changed = selected_channel != self._score_series_channel
        if channel_changed:
            self._clear_score_inspection_state()
        self._refresh_cached_rendering()
        self._update_overlay_geometry()
        self._refresh_score_series_for_current_channel()
        self._refresh_score_title()

    def flush_pending_live_refresh(self) -> None:
        pending_cache_key = self._pending_live_flush_key
        if pending_cache_key is None:
            return

        self._clear_pending_live_flush()
        resolved_render_target = self._resolve_render_target()
        if resolved_render_target is None:
            return

        cache_key, cache_entry, payload = resolved_render_target
        if cache_key != pending_cache_key:
            return

        self._render_payload(cache_key, cache_entry, payload)
        self._update_overlay_geometry()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._clear_score_inspection_state()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.canvas.heatmap.plot_widget.viewport():
            if self._analysis_selection_mode:
                handled = self._handle_analysis_selection_event(event)
                if handled:
                    return True
        return super().eventFilter(watched, event)

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
            and self._review_payload_matches_active_context(self._review_payload)
            and bool(getattr(self._review_payload, "manual_reanalysis_allowed", False))
            and self._factory_state.run_phase == "reviewing"
        )

    def _on_analysis_select_toggled(self, checked: bool) -> None:
        self._set_analysis_selection_mode(bool(checked))

    def _on_heatmap_view_range_changed(self, *_args) -> None:
        self._update_overlay_geometry()

    def _set_analysis_selection_mode(
        self,
        enabled: bool,
        *,
        refresh_overlay: bool = True,
    ) -> None:
        enabled = bool(enabled) and self._can_use_analysis_selection_mode()
        if self._analysis_selection_mode == enabled:
            self._sync_analysis_selection_button_checked()
            self._update_selection_cursor()
            if refresh_overlay:
                self._update_overlay_geometry()
            return

        self._analysis_selection_mode = enabled
        self._analysis_drag_start_sec = None
        self.canvas.heatmap.set_score_inspection_enabled(not enabled)
        self.canvas.heatmap.chart_interactions.set_left_box_zoom_enabled(not enabled)
        if enabled:
            self._clear_score_inspection_state()
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._sync_analysis_selection_button_checked()
        self._update_selection_cursor()
        if refresh_overlay:
            self._update_overlay_geometry()

    def _sync_analysis_selection_button_checked(self) -> None:
        checked = bool(self._analysis_selection_mode)
        text = "取消框选" if checked else "框选分析"
        text_changed = self.analysis_select_button.text() != text
        if text_changed:
            self.analysis_select_button.setText(text)
        if self.analysis_select_button.isChecked() == checked:
            if text_changed:
                self._layout_analysis_select_button()
            return
        previous = self.analysis_select_button.blockSignals(True)
        self.analysis_select_button.setChecked(checked)
        self.analysis_select_button.blockSignals(previous)
        self._layout_analysis_select_button()

    def _can_use_analysis_selection_mode(self) -> bool:
        return (
            self._review_payload_matches_active_context(self._review_payload)
            and bool(getattr(self._review_payload, "manual_reanalysis_allowed", False))
            and self._factory_state.run_phase == "reviewing"
            and self._current_overlay_geometry() is not None
        )

    def _handle_analysis_selection_event(self, event) -> bool:
        event_type = event.type()
        if event_type in {QEvent.Type.MouseMove, QEvent.Type.HoverMove}:
            if self._analysis_drag_start_sec is None:
                self._clear_hover_score()
                return True
            end_sec = self._time_sec_for_heatmap_viewport_event(
                event,
                clamp_to_plot=True,
            )
            if end_sec is not None:
                self._update_analysis_selection_from_drag(end_sec)
            return True

        if event_type == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            start_sec = self._time_sec_for_heatmap_viewport_event(
                event,
                clamp_to_plot=False,
            )
            self._clear_score_inspection_state()
            if start_sec is not None:
                self._analysis_drag_start_sec = start_sec
            return True

        if event_type == QEvent.Type.MouseButtonRelease:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            if self._analysis_drag_start_sec is not None:
                end_sec = self._time_sec_for_heatmap_viewport_event(
                    event,
                    clamp_to_plot=True,
                )
                if end_sec is not None:
                    self._update_analysis_selection_from_drag(end_sec)
            self._analysis_drag_start_sec = None
            return True

        if event_type == QEvent.Type.Leave:
            self._clear_hover_score()
            return True

        return False

    def _begin_analysis_selection_from_selection_box_event(self, event) -> None:
        position = _AnalysisSelectionBox._event_position(event)
        if position is None:
            return
        start_sec = self._time_sec_for_selection_box_pos(
            position,
            clamp_to_plot=False,
        )
        self._clear_score_inspection_state()
        if start_sec is not None:
            self._analysis_drag_start_sec = start_sec

    def _update_analysis_selection_from_selection_box_event(self, event) -> None:
        if self._analysis_drag_start_sec is None:
            self._clear_hover_score()
            return
        end_sec = self._time_sec_for_selection_box_event(
            event,
            clamp_to_plot=True,
        )
        if end_sec is not None:
            self._update_analysis_selection_from_drag(end_sec)

    def _finish_analysis_selection_from_selection_box_event(self, event) -> None:
        if self._analysis_drag_start_sec is not None:
            self._update_analysis_selection_from_selection_box_event(event)
        self._analysis_drag_start_sec = None

    def _time_sec_for_selection_box_event(
        self,
        event,
        *,
        clamp_to_plot: bool,
    ) -> float | None:
        global_position = getattr(event, "globalPosition", None)
        if callable(global_position):
            canvas_pos = self.canvas.mapFromGlobal(global_position().toPoint())
        else:
            global_pos = getattr(event, "globalPos", None)
            if not callable(global_pos):
                return None
            canvas_pos = self.canvas.mapFromGlobal(global_pos())

        heatmap_rect = self.canvas.heatmap.geometry()
        heatmap_pos = canvas_pos - heatmap_rect.topLeft()
        plot_rect = self.canvas.heatmap.plot_area_rect_in_widget()
        if plot_rect is None:
            return None
        if clamp_to_plot:
            x = max(plot_rect.left(), min(heatmap_pos.x(), plot_rect.right()))
        else:
            if not plot_rect.contains(heatmap_pos):
                return None
            x = heatmap_pos.x()
        return self.canvas.heatmap.time_sec_for_widget_x(float(x))

    def _time_sec_for_heatmap_viewport_event(
        self,
        event,
        *,
        clamp_to_plot: bool,
    ) -> float | None:
        position = getattr(event, "position", None)
        if not callable(position):
            return None
        return self._time_sec_for_heatmap_viewport_pos(
            position().toPoint(),
            clamp_to_plot=clamp_to_plot,
        )

    def _time_sec_for_heatmap_viewport_pos(
        self,
        position: QPoint,
        *,
        clamp_to_plot: bool,
    ) -> float | None:
        heatmap = self.canvas.heatmap
        plot_rect = heatmap.plot_area_rect_in_widget()
        if plot_rect is None:
            return None

        heatmap_pos = heatmap.plot_widget.viewport().mapTo(heatmap, position)
        if clamp_to_plot:
            x = max(plot_rect.left(), min(heatmap_pos.x(), plot_rect.right()))
        else:
            if not plot_rect.contains(heatmap_pos):
                return None
            x = heatmap_pos.x()
        return heatmap.time_sec_for_widget_x(float(x))

    def _update_analysis_selection_from_drag(self, end_sec: float) -> None:
        start_sec = self._analysis_drag_start_sec
        if start_sec is None:
            return
        window = self._analysis_window_from_drag(start_sec, end_sec)
        if window is None:
            return
        self._selected_window = window
        self._update_overlay_geometry()

    def _analysis_window_from_drag(
        self,
        start_sec: float,
        end_sec: float,
    ) -> tuple[float, float] | None:
        geometry = self._current_overlay_geometry()
        if geometry is None:
            return None

        try:
            start = float(start_sec)
            end = float(end_sec)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(start) or not np.isfinite(end):
            return None

        duration = max(
            abs(end - start),
            self._MIN_MANUAL_SELECTION_DURATION_SEC,
        )
        if end >= start:
            requested_window = (start, start + duration)
        else:
            requested_window = (start - duration, start)
        return self._normalized_selection_window(
            requested_window,
            geometry=geometry,
        )

    def _on_factory_state_changed(self, state):
        previous_config_id = self._factory_state.displayed_config_id
        previous_phase = self._factory_state.run_phase
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()

        config_switched = self._factory_state.displayed_config_id != previous_config_id
        phase_left_review = (
            previous_phase in self._REVIEW_PHASES
            and self._factory_state.run_phase not in self._REVIEW_PHASES
        )
        if config_switched or phase_left_review:
            self._clear_score_inspection_state()

        if not self._has_display_context():
            self._display_run_id = None
            self._display_run_id_locked_by_runtime = False
        elif config_switched:
            self._display_run_id = self._review_run_id_for_displayed_config()
            self._display_run_id_locked_by_runtime = False

        self._refresh_cached_rendering()
        self._update_overlay_geometry()
        self._refresh_score_series_for_current_channel()
        self._refresh_score_title()

    def _on_factory_review_changed(self, review):
        previous_review_context = (
            self._review_payload.displayed_config_id,
            self._review_payload.run_id,
        )
        if isinstance(review, FactoryReviewPayload):
            next_review_context = (review.displayed_config_id, review.run_id)
            review_context_changed = next_review_context != previous_review_context
            if review_context_changed:
                self._clear_score_inspection_state()
            if self._should_preserve_selection(review):
                self._review_payload = review
                self._sync_display_run_id_from_review(review)
                self._refresh_score_payloads_from_review(review)
                self._refresh_current_trace_payloads_from_review(review)
                self._seed_imported_stft_cache_from_review(review)
                self._refresh_cached_rendering()
                self._update_overlay_geometry()
                self._refresh_score_series_for_current_channel()
                self._refresh_score_title()
                return
            self._review_payload = review
        else:
            self._clear_score_inspection_state()
            self._review_payload = FactoryReviewPayload()

        self._sync_display_run_id_from_review(self._review_payload)
        self._selected_window = self._selection_from_review(self._review_payload)
        self._refresh_score_payloads_from_review(self._review_payload)
        self._refresh_current_trace_payloads_from_review(self._review_payload)
        self._seed_imported_stft_cache_from_review(self._review_payload)
        self._refresh_cached_rendering()
        self._update_overlay_geometry()
        self._refresh_score_series_for_current_channel()
        self._refresh_score_title()

    def _on_factory_display_reset(self, _reason: str):
        self._set_analysis_selection_mode(False)
        self._review_payload = FactoryReviewPayload()
        self._selected_window = None
        self._drag_offset_px = None
        self._display_run_id = None
        self._display_run_id_locked_by_runtime = False
        self._score_payload_by_channel.clear()
        self._current_trace_payload_by_channel.clear()
        self._live_current_trace_payload_by_run.clear()
        self._score_times_sec = []
        self._score_values = []
        self._score_series_channel = None
        self._clear_score_inspection_state()
        self._clear_pending_live_flush()
        self._stft_cache.clear()
        self._clear_rendering()
        self._update_overlay_geometry()
        self._set_score_title(self._SCORE_TITLE_PLACEHOLDER)

    def _on_factory_runtime_context(self, payload) -> None:
        config_id = getattr(payload, "config_id", None)
        run_id = getattr(payload, "run_id", None)
        if (
            config_id == self._factory_state.displayed_config_id
            and isinstance(run_id, str)
            and run_id
        ):
            run_switched = (
                self._display_run_id is not None and self._display_run_id != run_id
            )
            if run_switched:
                self._clear_score_inspection_state()
            self._display_run_id = run_id
            self._display_run_id_locked_by_runtime = True
            if (
                run_switched
                and self._current_trace_for_channel(self._selected_display_channel())
                is None
            ):
                self.canvas.heatmap.set_current_trace(None)
            self._refresh_cached_rendering()
            self._refresh_score_series_for_current_channel()
            self._refresh_score_title()

    def _on_current_trace_updated(self, config_id: int, run_id: str, current_trace) -> None:
        if not isinstance(run_id, str) or not run_id:
            return
        if isinstance(config_id, bool):
            return
        try:
            normalized_config_id = int(config_id)
        except (TypeError, ValueError):
            return
        if not isinstance(current_trace, dict):
            return

        if (
            not self._has_display_context()
            or normalized_config_id != self._factory_state.displayed_config_id
        ):
            return

        key = (normalized_config_id, run_id)
        if self._display_run_id is None:
            self._live_current_trace_payload_by_run[key] = dict(current_trace)
            return

        if run_id != self._display_run_id:
            return

        self._live_current_trace_payload_by_run[key] = dict(current_trace)

        if (
            self._render_cache_key is not None
            and self._render_cache_key[:2] == key
            and self.canvas.heatmap.has_image()
        ):
            self.canvas.heatmap.set_current_trace(
                self._current_trace_for_channel(self._render_cache_key[2])
            )
            self._update_overlay_geometry()
            return

        if self._resolve_render_target() is None:
            return

        self._refresh_cached_rendering()
        self._update_overlay_geometry()

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
            self._display_run_id_locked_by_runtime = True
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

    def _seed_imported_stft_cache_from_review(
        self,
        review: FactoryReviewPayload,
    ) -> None:
        if getattr(review, "trigger_source", None) != "import_audio":
            return

        config_id = getattr(review, "displayed_config_id", None)
        run_id = getattr(review, "run_id", None)
        chart_payload = getattr(review, "chart_payload", None)
        if (
            not isinstance(config_id, int)
            or isinstance(config_id, bool)
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(chart_payload, dict)
        ):
            return

        seeded_channels: set[int] = set()
        has_imported_stft_payload = False
        seen_channels: set[int] = set()
        for channel, channel_payload in chart_payload.items():
            try:
                channel_index = int(channel)
            except (TypeError, ValueError):
                continue
            if channel_index in seen_channels or not isinstance(channel_payload, dict):
                continue
            seen_channels.add(channel_index)

            stft_payload = channel_payload.get("stft")
            if not isinstance(stft_payload, dict):
                continue
            has_imported_stft_payload = True
            parsed_payload = self._validated_imported_stft_payload(stft_payload)
            if parsed_payload is None:
                continue

            times_sec, frequencies_hz, magnitude_db = parsed_payload
            cache_entry = _StftCacheEntry(snap_initial_to_zero=False)
            for column_index, time_sec in enumerate(times_sec):
                cache_entry.append_frame(
                    StftFrame(
                        time=float(time_sec),
                        frequencies=frequencies_hz,
                        magnitudes=magnitude_db[:, column_index],
                    )
                )
            if cache_entry.has_data():
                self._stft_cache[(config_id, run_id, channel_index)] = cache_entry
                seeded_channels.add(channel_index)

        if not has_imported_stft_payload:
            return

        for cache_key in list(self._stft_cache):
            cache_config_id, cache_run_id, cache_channel_index = cache_key
            if (
                cache_config_id == config_id
                and cache_run_id == run_id
                and cache_channel_index not in seeded_channels
            ):
                del self._stft_cache[cache_key]

    @staticmethod
    def _validated_imported_stft_payload(
        stft_payload: dict,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        try:
            times_sec = np.asarray(stft_payload.get("times_sec"), dtype=float)
            frequencies_hz = np.asarray(
                stft_payload.get("frequencies_hz"),
                dtype=float,
            )
            magnitude_db = np.asarray(stft_payload.get("magnitude_db"), dtype=float)
        except (TypeError, ValueError):
            return None

        if (
            times_sec.ndim != 1
            or times_sec.size == 0
            or not np.all(np.isfinite(times_sec))
            or frequencies_hz.ndim != 1
            or frequencies_hz.size == 0
            or not np.all(np.isfinite(frequencies_hz))
            or magnitude_db.ndim != 2
            or magnitude_db.shape != (frequencies_hz.size, times_sec.size)
            or not np.all(np.isfinite(magnitude_db))
        ):
            return None

        return times_sec, frequencies_hz, magnitude_db

    def _replay_selection(self):
        context = self._replay_context()
        if context is None:
            return

        test_service = getattr(self.services, "test", None)
        replay_selection = getattr(test_service, "replay_selection", None)
        if not callable(replay_selection):
            self._emit_replay_failure()
            return

        if not bool(replay_selection(*context)):
            self._emit_replay_failure()

    def _selected_display_channel(self) -> int | None:
        provider = self.selected_display_channel_provider
        if not callable(provider):
            return None

        try:
            selected_channel = provider()
        except Exception:
            return None

        if isinstance(selected_channel, bool):
            return None
        try:
            return int(selected_channel)
        except (TypeError, ValueError):
            return None

    def _refresh_score_payloads_from_review(
        self,
        review: FactoryReviewPayload,
    ) -> None:
        self._score_payload_by_channel.clear()
        chart_payload = getattr(review, "chart_payload", None)
        if not isinstance(chart_payload, dict):
            return

        for channel_key, channel_payload in chart_payload.items():
            try:
                channel_index = int(channel_key)
            except (TypeError, ValueError):
                continue
            if channel_index in self._score_payload_by_channel:
                continue
            if not isinstance(channel_payload, dict):
                continue
            score_payload = channel_payload.get("stft_score")
            if isinstance(score_payload, dict):
                self._score_payload_by_channel[channel_index] = score_payload

    def _refresh_current_trace_payloads_from_review(
        self,
        review: FactoryReviewPayload,
    ) -> None:
        self._current_trace_payload_by_channel.clear()
        chart_payload = getattr(review, "chart_payload", None)
        if not isinstance(chart_payload, dict):
            return

        for channel_key, channel_payload in chart_payload.items():
            try:
                channel_index = int(channel_key)
            except (TypeError, ValueError):
                continue
            if channel_index in self._current_trace_payload_by_channel:
                continue
            if not isinstance(channel_payload, dict):
                continue
            current_trace = channel_payload.get("current_trace")
            if isinstance(current_trace, dict):
                self._current_trace_payload_by_channel[channel_index] = current_trace

    def _refresh_score_title(self) -> None:
        self._set_score_title(self._score_title_for_current_context())

    def _set_score_title(self, text: str) -> None:
        normalized = str(text)
        if normalized == self._current_score_text:
            return
        self._current_score_text = normalized
        self.score_title_changed.emit(normalized)

    def _score_title_for_current_context(self) -> str:
        if not self._review_payload_matches_score_context(self._review_payload):
            return self._SCORE_TITLE_PLACEHOLDER

        selected_channel = self._selected_display_channel()
        if selected_channel is None:
            return self._SCORE_TITLE_PLACEHOLDER

        return self._score_title_from_payload(
            self._score_payload_by_channel.get(selected_channel)
        )

    def _review_payload_matches_score_context(
        self,
        review: FactoryReviewPayload,
    ) -> bool:
        return self._review_payload_matches_active_context(review)

    def _review_payload_matches_active_context(
        self,
        review: FactoryReviewPayload,
    ) -> bool:
        if self._factory_state.run_phase not in self._REVIEW_PHASES:
            return False
        return self._review_payload_matches_display_context(review)

    def _review_payload_matches_display_context(
        self,
        review: FactoryReviewPayload,
    ) -> bool:
        if review.displayed_config_id != self._factory_state.displayed_config_id:
            return False
        if not isinstance(review.run_id, str) or not review.run_id:
            return False
        return self._display_run_id is None or review.run_id == self._display_run_id

    def _current_trace_for_channel(self, channel_index: int | None) -> dict | None:
        if channel_index is None:
            return None
        if self._review_payload_matches_score_context(self._review_payload):
            return self._current_trace_payload_by_channel.get(channel_index)
        if self._factory_state.run_phase not in {"running", "analyzing"}:
            return None
        config_id = self._factory_state.displayed_config_id
        run_id = self._display_run_id
        if config_id is None or not isinstance(run_id, str) or not run_id:
            return None
        return self._live_current_trace_payload_by_run.get((config_id, run_id))

    def _score_title_from_payload(self, score_payload: dict | None) -> str:
        if not isinstance(score_payload, dict):
            return self._SCORE_TITLE_PLACEHOLDER
        if score_payload.get("enabled") is False:
            return self._SCORE_TITLE_PLACEHOLDER

        try:
            summary_score = float(score_payload.get("summary_score"))
        except (TypeError, ValueError):
            return self._SCORE_TITLE_PLACEHOLDER
        if not np.isfinite(summary_score):
            return self._SCORE_TITLE_PLACEHOLDER
        return f"得分：{summary_score:.2f}"

    def _refresh_score_series_for_current_channel(self) -> None:
        previous_series = (
            self._score_series_channel,
            tuple(self._score_times_sec),
            tuple(self._score_values),
        )
        selected_channel = self._selected_display_channel()
        self._score_series_channel = selected_channel
        self._score_times_sec = []
        self._score_values = []

        if (
            selected_channel is not None
            and self._review_payload_matches_score_context(self._review_payload)
        ):
            score_payload = self._score_payload_by_channel.get(selected_channel)
            series = self._score_series_from_payload(score_payload)
            if series is not None:
                self._score_times_sec, self._score_values = series

        current_series = (
            self._score_series_channel,
            tuple(self._score_times_sec),
            tuple(self._score_values),
        )
        if current_series != previous_series:
            self._clear_score_inspection_state()

    def _score_series_from_payload(
        self,
        score_payload: dict | None,
    ) -> tuple[list[float], list[float]] | None:
        if not isinstance(score_payload, dict):
            return None
        if score_payload.get("enabled") is False:
            return None

        try:
            times_sec = np.asarray(score_payload.get("times_sec"), dtype=float)
            scores = np.asarray(score_payload.get("scores"), dtype=float)
        except (TypeError, ValueError):
            return None

        if (
            times_sec.ndim != 1
            or scores.ndim != 1
            or times_sec.size == 0
            or times_sec.size != scores.size
            or not np.all(np.isfinite(times_sec))
            or not np.all(np.isfinite(scores))
        ):
            return None

        return (times_sec.astype(float).tolist(), scores.astype(float).tolist())

    def _nearest_score_index(self, time_sec: float) -> int | None:
        if not self._score_times_sec or not self._score_values:
            return None

        try:
            requested_time = float(time_sec)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(requested_time):
            return None

        times = np.asarray(self._score_times_sec, dtype=float)
        nearest_index = int(np.abs(times - requested_time).argmin())
        frame_step = self._score_frame_step()
        threshold = max(frame_step * 0.75, 0.05)
        if abs(float(times[nearest_index]) - requested_time) > threshold:
            return None
        return nearest_index

    def _score_frame_step(self) -> float:
        times = np.asarray(self._score_times_sec, dtype=float)
        if times.size > 1:
            deltas = np.diff(times)
            positive = deltas[np.isfinite(deltas) & (deltas > 0.0)]
            if positive.size > 0:
                return float(np.median(positive))

        rendered_extent = self.canvas.heatmap.rendered_time_extent_sec()
        if rendered_extent is not None:
            rendered_start_sec, rendered_end_sec = rendered_extent
            rendered_span_sec = float(rendered_end_sec) - float(rendered_start_sec)
            if np.isfinite(rendered_span_sec) and rendered_span_sec > 0.0:
                return rendered_span_sec / max(1, len(self._score_times_sec))

        return 0.5

    def _score_text_for_index(self, index: int | None) -> str | None:
        if index is None:
            return None
        if index < 0 or index >= len(self._score_values):
            return None
        score = float(self._score_values[index])
        if not np.isfinite(score):
            return None
        return f"得分：{score:.2f}"

    def _show_hover_score_for_time(self, time_sec: float) -> None:
        if self._analysis_selection_mode:
            return
        self._hover_score_index = self._nearest_score_index(time_sec)
        self._update_score_overlay_label()

    def _pin_score_for_time(self, time_sec: float) -> None:
        if self._analysis_selection_mode:
            return
        score_index = self._nearest_score_index(time_sec)
        if score_index is None:
            return
        self._pinned_score_index = score_index
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._update_score_overlay_label()

    def _clear_pinned_score(self) -> None:
        self._pinned_score_index = None
        self._hover_score_index = None
        self._update_score_overlay_label()

    def _clear_hover_score(self) -> None:
        self._hover_score_index = None
        self._update_score_overlay_label()

    def _clear_score_inspection_state(self) -> None:
        self._pinned_score_index = None
        self._hover_score_index = None
        if hasattr(self, "score_overlay_label"):
            self.score_overlay_label.hide()

    def _update_score_overlay_label(self) -> None:
        score_index = (
            self._pinned_score_index
            if self._pinned_score_index is not None
            else self._hover_score_index
        )
        score_text = self._score_text_for_index(score_index)
        if score_text is None:
            self.score_overlay_label.hide()
            return

        self.score_overlay_label.setText(score_text)
        self.score_overlay_label.adjustSize()
        self._layout_score_overlay_label()
        self.score_overlay_label.show()
        self.score_overlay_label.raise_()

    def _layout_score_overlay_label(self) -> None:
        if not hasattr(self, "score_overlay_label"):
            return

        label_size = self.score_overlay_label.sizeHint()
        width = max(label_size.width(), 80)
        height = max(label_size.height(), 28)
        geometry = self._current_overlay_geometry()
        if geometry is None:
            target_rect = self.canvas.rect()
        else:
            target_rect = geometry.plot_rect

        margin = 8
        canvas_rect = self.canvas.rect()
        occupied_rects = []
        top_control_rects = []
        for widget in (
            self.selection_box,
            self.analysis_select_button,
            self.analyze_button,
            self.replay_button,
        ):
            if not widget.isVisible():
                continue
            rect = self._widget_rect_in_canvas(widget)
            occupied_rects.append(rect)
            if widget is not self.selection_box:
                top_control_rects.append(rect)
        top_band_bottom = max(
            (
                rect.bottom()
                for rect in top_control_rects
                if rect.top() <= target_rect.top() + height
            ),
            default=target_rect.top(),
        )

        x_candidates = [
            target_rect.right() - width - margin + 1,
            target_rect.left() + margin,
            canvas_rect.right() - width - margin + 1,
            canvas_rect.left() + margin,
        ]
        y_candidates = [
            target_rect.top() + margin,
            top_band_bottom + margin + 1,
            target_rect.bottom() - height - margin + 1,
            target_rect.bottom() + margin,
            canvas_rect.bottom() - height - margin + 1,
            canvas_rect.top() + margin,
        ]
        for rect in occupied_rects:
            x_candidates.extend(
                (rect.right() + margin + 1, rect.left() - width - margin)
            )
            y_candidates.extend(
                (rect.bottom() + margin + 1, rect.top() - height - margin)
            )

        fallback_rect = None
        for x_candidate in x_candidates:
            for y_candidate in y_candidates:
                rect = self._bounded_score_overlay_rect(
                    x_candidate,
                    y_candidate,
                    width=width,
                    height=height,
                    canvas_rect=canvas_rect,
                    margin=margin,
                )
                if fallback_rect is None:
                    fallback_rect = rect
                if all(not rect.intersects(occupied) for occupied in occupied_rects):
                    self.score_overlay_label.setGeometry(rect)
                    return

        if fallback_rect is None:
            fallback_rect = QRect(canvas_rect.left(), canvas_rect.top(), width, height)
        self.score_overlay_label.setGeometry(fallback_rect)

    def _widget_rect_in_canvas(self, widget: QWidget) -> QRect:
        if widget.parentWidget() is self.canvas:
            return QRect(widget.geometry())
        top_left = widget.mapTo(self.canvas, QPoint(0, 0))
        return QRect(top_left, widget.size())

    def _bounded_score_overlay_rect(
        self,
        x: int,
        y: int,
        *,
        width: int,
        height: int,
        canvas_rect: QRect,
        margin: int,
    ) -> QRect:
        min_x = canvas_rect.left() + margin
        max_x = canvas_rect.right() - width - margin + 1
        min_y = canvas_rect.top() + margin
        max_y = canvas_rect.bottom() - height - margin + 1
        if max_x < min_x:
            min_x = canvas_rect.left()
            max_x = max(min_x, canvas_rect.right() - width + 1)
        if max_y < min_y:
            min_y = canvas_rect.top()
            max_y = max(min_y, canvas_rect.bottom() - height + 1)

        bounded_x = max(min_x, min(int(x), max_x))
        bounded_y = max(min_y, min(int(y), max_y))
        return QRect(bounded_x, bounded_y, int(width), int(height))

    def _replay_context(self) -> tuple[int, str, int, float, float] | None:
        if (
            self._factory_state.run_phase not in self._REVIEW_PHASES
            or self._selected_window is None
            or not self._review_payload_matches_active_context(self._review_payload)
        ):
            return None

        displayed_config_id = self._factory_state.displayed_config_id
        if isinstance(displayed_config_id, bool):
            return None
        try:
            config_id = int(displayed_config_id)
        except (TypeError, ValueError):
            return None

        run_id = self._display_run_id or self._review_run_id_for_displayed_config()
        if not isinstance(run_id, str) or not run_id:
            return None

        display_channel = self._selected_display_channel()
        if display_channel is None:
            return None

        validated = self._validated_window(
            start_sec=self._selected_window[0],
            end_sec=self._selected_window[1],
            captured_duration_sec=self._captured_duration_sec(),
        )
        if validated is None:
            return None

        return (config_id, run_id, display_channel, validated[0], validated[1])

    def _can_replay_selection(self) -> bool:
        context = self._replay_context()
        if context is None:
            return False

        test_service = getattr(self.services, "test", None)
        can_replay_selection = getattr(test_service, "can_replay_selection", None)
        if not callable(can_replay_selection):
            return False
        return bool(can_replay_selection(*context))

    def _emit_replay_failure(self) -> None:
        bus = getattr(self.services, "bus", None)
        for signal_name in ("status", "log"):
            signal = getattr(bus, signal_name, None)
            emit = getattr(signal, "emit", None)
            if callable(emit):
                emit("重放失败")

    def _selection_from_review(
        self,
        review: FactoryReviewPayload,
    ) -> tuple[float, float] | None:
        if (
            review.displayed_config_id is None
            or not self._review_payload_matches_display_context(review)
        ):
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
        review_matches_active_context = self._review_payload_matches_active_context(
            self._review_payload
        )
        manual_allowed = bool(
            review_matches_active_context
            and getattr(self._review_payload, "manual_reanalysis_allowed", False)
        )
        has_review_window = (
            self._selected_window is not None
            and in_review_phase
            and review_matches_active_context
        )
        geometry = (
            self._current_overlay_geometry()
            if (has_review_window or manual_allowed)
            else None
        )
        selection_tool_visible = in_review_phase and manual_allowed and geometry is not None
        selection_tool_enabled = (
            selection_tool_visible and self._factory_state.run_phase == "reviewing"
        )
        self.analysis_select_button.setVisible(selection_tool_visible)
        self.analysis_select_button.setEnabled(selection_tool_enabled)
        if not selection_tool_visible and self._analysis_selection_mode:
            self._set_analysis_selection_mode(False, refresh_overlay=False)
        else:
            self._sync_analysis_selection_button_checked()

        show_selection = (
            has_review_window
            and self._analysis_selection_mode
            and self._factory_state.run_phase == "reviewing"
        )
        self.selection_box.setVisible(show_selection)
        self.selection_box.set_selection_mode_active(self._analysis_selection_mode)

        button_visible = show_selection and manual_allowed
        self.analyze_button.setVisible(button_visible)
        self.analyze_button.setEnabled(
            button_visible and self._factory_state.run_phase == "reviewing"
        )
        self.replay_button.setVisible(show_selection)
        self.replay_button.setEnabled(False)

        self._update_selection_cursor()
        if not has_review_window:
            self._layout_analysis_select_button()
            self._layout_score_overlay_label()
            return

        if geometry is None:
            self.selection_box.hide()
            self.analyze_button.hide()
            self.replay_button.hide()
            self._layout_analysis_select_button()
            self._layout_score_overlay_label()
            return

        normalized_window = self._normalized_selection_window(
            self._selected_window,
            geometry=geometry,
        )
        if normalized_window is None:
            self.selection_box.hide()
            self.analyze_button.hide()
            self.replay_button.hide()
            self._layout_analysis_select_button()
            self._layout_score_overlay_label()
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
            self.replay_button.hide()
            self._layout_analysis_select_button()
            self._layout_score_overlay_label()
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

        if not show_selection:
            self.selection_box.hide()
            self._layout_analysis_select_button()
            self._layout_score_overlay_label()
            return

        self.replay_button.setEnabled(self._can_replay_selection())
        self._layout_selection_buttons(
            left=left,
            width=width,
            plot_rect=plot_rect,
            button_visible=button_visible,
        )
        self._layout_analysis_select_button()
        self._layout_score_overlay_label()

    def _layout_analysis_select_button(self) -> None:
        if not self.analysis_select_button.isVisible():
            return

        host = self._ensure_analysis_select_button_host()
        margin = 4
        spacing = 6
        size_hint = self.analysis_select_button.sizeHint()
        width = max(size_hint.width() + 28, 144)
        height = max(size_hint.height(), 28)
        reset_button = getattr(
            getattr(self.canvas.heatmap, "chart_interactions", None),
            "reset_button",
            None,
        )
        module_offset_x = 0
        if host is not self:
            module_offset_x = self.geometry().x()
        x = module_offset_x + margin
        if reset_button is not None and reset_button.isVisible():
            x = module_offset_x + reset_button.x() + reset_button.width() + spacing
        if host is self:
            y = self.height() - height - margin
        else:
            y = host.height() - height - margin
        y = max(margin, y)
        self.analysis_select_button.setGeometry(x, y, width, height)
        self.analysis_select_button.raise_()

    def _ensure_analysis_select_button_host(self) -> QWidget:
        host = self.parentWidget()
        if host is None or host.objectName() != "Card":
            host = self
        if self.analysis_select_button.parentWidget() is host:
            return host

        was_visible = self.analysis_select_button.isVisible()
        self.analysis_select_button.setParent(host)
        if was_visible:
            self.analysis_select_button.show()
        return host

    def _layout_selection_buttons(
        self,
        *,
        left: int,
        width: int,
        plot_rect: QRect,
        button_visible: bool,
    ) -> None:
        buttons = []
        if button_visible:
            buttons.append(self.analyze_button)
        if self.replay_button.isVisible():
            buttons.append(self.replay_button)
        if not buttons:
            return

        spacing = 8
        button_sizes = []
        for button in buttons:
            size_hint = button.sizeHint()
            button_sizes.append((max(size_hint.width() + 6, 58), size_hint.height()))

        total_width = sum(button_width for button_width, _ in button_sizes)
        total_width += spacing * (len(buttons) - 1)
        right_group_x = left + width + spacing
        left_group_x = left - total_width - spacing
        if right_group_x + total_width - 1 <= plot_rect.right():
            group_x = right_group_x
        elif left_group_x >= plot_rect.left():
            group_x = left_group_x
        else:
            latest_group_x = plot_rect.right() - total_width + 1
            group_x = max(
                plot_rect.left(),
                min(left + spacing, latest_group_x),
            )

        current_x = group_x
        for button, (button_width, button_height) in zip(buttons, button_sizes):
            button_y = max(
                plot_rect.top(),
                min(plot_rect.top() + 8, plot_rect.bottom() - button_height + 1),
            )
            button.setGeometry(current_x, button_y, button_width, button_height)
            current_x += button_width + spacing

    def _update_selection_cursor(self) -> None:
        self.selection_box.set_selection_mode_active(self._analysis_selection_mode)
        if self._analysis_selection_mode:
            self.selection_box.setCursor(Qt.CursorShape.CrossCursor)
            self.canvas.heatmap.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.selection_box.setCursor(Qt.CursorShape.ArrowCursor)
            self.canvas.heatmap.unsetCursor()

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

    def _load_displayed_config(self) -> dict:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return {}

        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if not callable(load_config):
            return {}

        return load_config(config_id)

    def _fixed_color_levels_for_channel(
        self,
        channel_index: int | None,
    ) -> tuple[float, float] | None:
        if channel_index is None:
            return None

        try:
            config = self._load_displayed_config()
        except Exception:
            return None

        if not isinstance(config, dict):
            return None

        channels = config.get("channels", [])
        if not isinstance(channels, list):
            return None

        for channel in channels:
            if not isinstance(channel, dict):
                continue
            if channel.get("channel_index") != channel_index:
                continue
            if not bool(channel.get("stft_color_scale_enabled", False)):
                return None
            try:
                lower = float(channel.get("stft_color_scale_min_db"))
                upper = float(channel.get("stft_color_scale_max_db"))
            except (TypeError, ValueError):
                return None
            if not np.isfinite(lower) or not np.isfinite(upper) or lower >= upper:
                return None
            return (lower, upper)
        return None

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
            if (
                self._display_run_id_locked_by_runtime
                and self._display_run_id is not None
                and self._display_run_id != review.run_id
            ):
                return
            if (
                self._factory_state.run_phase in {"running", "analyzing"}
                and self._display_run_id != review.run_id
            ):
                return
            self._display_run_id = review.run_id
            self._display_run_id_locked_by_runtime = False
        elif self._factory_state.run_phase in self._REVIEW_PHASES:
            if (
                review.displayed_config_id is not None
                and review.displayed_config_id != self._factory_state.displayed_config_id
            ):
                return
            self._display_run_id = None
            self._display_run_id_locked_by_runtime = False

    def _refresh_cached_rendering(self, *, boundary_render: bool = True) -> None:
        if boundary_render:
            self._clear_pending_live_flush()

        resolved_render_target = self._resolve_render_target()
        if resolved_render_target is None:
            self._clear_rendering()
            return

        cache_key, cache_entry, payload = resolved_render_target
        self._render_payload(cache_key, cache_entry, payload)

    def _resolve_render_target(
        self,
    ) -> tuple[tuple[int, str, int], _StftCacheEntry, dict] | None:
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

        return (cache_key, cache_entry, payload)

    def _render_payload(
        self,
        cache_key: tuple[int, str, int],
        cache_entry: _StftCacheEntry,
        payload: dict,
    ) -> None:
        levels_db = self._fixed_color_levels_for_channel(cache_key[2])
        current_trace = self._current_trace_for_channel(cache_key[2])
        render_signature = (
            cache_key,
            cache_entry.render_revision,
            levels_db,
            cache_entry.snap_initial_to_zero,
        )
        has_image = self.canvas.heatmap.has_image()
        if render_signature == self._last_render_signature and has_image:
            self._render_cache_key = cache_key
            self.canvas.heatmap.set_current_trace(current_trace)
            return

        reset_levels = cache_key != self._render_cache_key or not has_image
        self._render_cache_key = cache_key
        self.canvas.heatmap.set_image(
            times_sec=payload["times_sec"],
            frequencies_hz=payload["frequencies_hz"],
            magnitude_db=payload["magnitude_db"],
            reset_levels=reset_levels,
            levels_db=levels_db,
            snap_initial_to_zero=cache_entry.snap_initial_to_zero,
        )
        self.canvas.heatmap.set_current_trace(current_trace)
        self._last_render_signature = render_signature

    def _clear_rendering(self) -> None:
        self._render_cache_key = None
        self._last_render_signature = None
        self._clear_score_inspection_state()
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
