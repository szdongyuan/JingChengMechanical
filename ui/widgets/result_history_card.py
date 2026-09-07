from __future__ import annotations

import re
from dataclasses import replace

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.service.result_history import (
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultPhase,
    ReviewDisplayPayload,
)


_CONCLUSION_STATE = {
    ResultConclusion.OK: "ok",
    ResultConclusion.NG: "ng",
    ResultConclusion.NO_RESULT: "no_result",
    ResultConclusion.ERROR: "error",
    ResultConclusion.CAPACITY_REJECTED: "capacity_rejected",
    ResultConclusion.MANUAL_STOPPED: "manual_stopped",
}

_RESULT_STATE_PALETTE = {
    "active": ("#0d47a1", "#e3f2fd"),
    "ok": ("#1b5e20", "#e8f5e9"),
    "ng": ("#b71c1c", "#ffebee"),
    "error": ("#880e4f", "#fce4ec"),
    "reanalyzing": ("#4a148c", "#f3e5f5"),
    "no_result": ("#424242", "#f5f5f5"),
    "capacity_rejected": ("#a63a00", "#fff3e0"),
    "manual_stopped": ("#4e342e", "#efebe9"),
}

_RESULT_STATE_ACCESSIBLE_TEXT = {
    "active": "进行中",
    "ok": "通过",
    "ng": "不通过",
    "error": "失败",
    "reanalyzing": "重新分析中",
    "no_result": "无结果",
    "capacity_rejected": "容量拒绝",
    "manual_stopped": "已手动停止",
}


def _card_style_sheet() -> str:
    frame_rules = "\n".join(
        (
            f'QFrame#result_history_card[resultState="{state}"] {{'
            f" border-left: 6px solid {foreground};"
            f" background: {background}; }}"
        )
        for state, (foreground, background) in _RESULT_STATE_PALETTE.items()
    )
    return f"""
        QFrame#result_history_card {{
            border: 1px solid #c8c8c8;
            border-radius: 5px;
            background: palette(base);
        }}
        {frame_rules}
        QFrame#result_history_card[selected="true"] {{
            border-top: 1px solid #0d47a1;
            border-right: 1px solid #0d47a1;
            border-bottom: 1px solid #0d47a1;
        }}
        QFrame#result_history_card:focus {{
            border-top: 1px dashed #0d47a1;
            border-right: 1px dashed #0d47a1;
            border-bottom: 1px dashed #0d47a1;
        }}
        QFrame#result_history_card[selectionEnabled="false"] {{
            background: #eceff1;
            color: #37474f;
        }}
    """


_CARD_STYLE_SHEET = _card_style_sheet()
_SUMMARY_ROWS = (
    ("spl", "声压峰值"),
    ("spl_average", "声压平均"),
    ("loudness", "响度峰值"),
    ("loudness_average", "响度平均"),
    ("stft_score", "频谱得分"),
    ("ai", "AI 分析"),
)
_SUMMARY_RESULT_TEXT = {
    "PASS": "通过",
    "FAIL": "不通过",
}
_CONCLUSION_TEXT = {
    ResultConclusion.OK: "通过",
    ResultConclusion.NG: "不通过",
    ResultConclusion.NO_RESULT: "无结果",
    ResultConclusion.ERROR: "失败",
    ResultConclusion.CAPACITY_REJECTED: "容量已满",
    ResultConclusion.MANUAL_STOPPED: "已停止",
}


def _result_state(snapshot: ResultHistoryCardSnapshot) -> str:
    if snapshot.phase is ResultPhase.REANALYZING:
        return "reanalyzing"
    if snapshot.phase is not ResultPhase.TERMINAL:
        return "active"
    return _CONCLUSION_STATE[snapshot.conclusion]


class _FittedSummaryTable(QTableWidget):
    """Fit wrapped result text while keeping the summary free of scrollbars."""

    _ROW_HEIGHT = 23

    def __init__(self, rows: int, columns: int, parent=None) -> None:
        super().__init__(rows, columns, parent)
        self._last_fitted_result_width = -1
        self._width_refit_timer = QTimer(self)
        self._width_refit_timer.setSingleShot(True)
        self._width_refit_timer.timeout.connect(self._run_width_refit)

    def schedule_initial_fit(self) -> None:
        self._fit_rows()
        self._schedule_width_refit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._schedule_width_refit()

    def _schedule_width_refit(self) -> None:
        if not self._width_refit_timer.isActive():
            self._width_refit_timer.start(0)

    def _run_width_refit(self) -> None:
        if self.columnWidth(3) != self._last_fitted_result_width:
            self._fit_rows()

    def _fit_rows(self) -> None:
        available_width = max(1, self.columnWidth(3) - 12)
        for row in range(self.rowCount()):
            item = self.item(row, 3)
            text = item.text() if item is not None else ""
            bounds = self.fontMetrics().boundingRect(
                QRect(0, 0, available_width, 10_000),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                text,
            )
            self.setRowHeight(
                row,
                max(self._ROW_HEIGHT, bounds.height() + 8),
            )
        self.setFixedHeight(
            self.horizontalHeader().height()
            + sum(self.rowHeight(row) for row in range(self.rowCount()))
            + self.frameWidth() * 2
            + 2
        )
        self._last_fitted_result_width = self.columnWidth(3)


class _FittedStatusLabel(QLabel):
    """Keep the complete status on one line at the largest readable size."""

    _MAX_PIXEL_SIZE = 32
    _MIN_PIXEL_SIZE = 18

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._color = "#424242"
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_text)

    def set_display(self, text: str, color: str) -> None:
        self._full_text = text
        self._color = color
        self.setText(text)
        self.setToolTip(f"结果摘要：{text}")
        self.setAccessibleDescription(f"结果摘要：{text}")
        self._apply_style(self._MAX_PIXEL_SIZE)
        self._fit_text()
        self._schedule_fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._schedule_fit()

    def _schedule_fit(self) -> None:
        if not self._fit_timer.isActive():
            self._fit_timer.start(0)

    def _fit_text(self) -> None:
        available_width = self.contentsRect().width()
        if available_width <= 0:
            return

        chosen_size = self._MIN_PIXEL_SIZE
        complete_text_fits = False
        for pixel_size in range(
            self._MAX_PIXEL_SIZE,
            self._MIN_PIXEL_SIZE - 1,
            -1,
        ):
            metrics = QFontMetrics(self._font_at_size(pixel_size))
            if metrics.horizontalAdvance(self._full_text) <= available_width:
                chosen_size = pixel_size
                complete_text_fits = True
                break

        self._apply_style(chosen_size)
        if complete_text_fits:
            visible_text = self._full_text
        else:
            metrics = QFontMetrics(self._font_at_size(chosen_size))
            visible_text = metrics.elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                available_width,
            )
        self.setText(visible_text)

    def _font_at_size(self, pixel_size: int) -> QFont:
        font = self.font()
        font.setPixelSize(pixel_size)
        font.setWeight(QFont.Weight.ExtraBold)
        return font

    def _apply_style(self, pixel_size: int) -> None:
        self.setStyleSheet(
            f"font-size: {pixel_size}px; font-weight: 800; "
            f"color: {self._color};"
        )


class ResultHistoryCard(QFrame):
    """Compact, store-independent projection of one history card snapshot."""

    activated = pyqtSignal(str)

    def __init__(
        self,
        snapshot: ResultHistoryCardSnapshot,
        parent=None,
        *,
        payload: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._run_id = snapshot.run_id
        self._snapshot = snapshot
        self._selected = False
        self._selection_enabled = True
        result_state = _result_state(snapshot)

        self.setObjectName("result_history_card")
        self.setAccessibleName("结果历史卡片")
        self.setAccessibleDescription(
            f"结果状态：{_RESULT_STATE_ACCESSIBLE_TEXT[result_state]}"
        )
        self.setToolTip("点击查看此结果")
        self.setProperty("run_id", snapshot.run_id)
        self.setProperty("resultState", result_state)
        self.setProperty("selected", False)
        self.setProperty("selectionEnabled", True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_STYLE_SHEET)

        layout = QVBoxLayout(self)
        self._manual_judgment_widget: QWidget | None = None
        self._manual_action_animation: QPropertyAnimation | None = None
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        config_label = self._add_label(
            layout,
            f"配置：{snapshot.config.name or '—'}",
            "result_history_card_config_name",
            "结果配置名称",
        )
        config_label.setWordWrap(False)
        config_label.setToolTip(config_label.text())
        metadata_layout = QHBoxLayout()
        metadata_layout.setContentsMargins(0, 0, 0, 0)
        metadata_layout.setSpacing(8)
        sn_label = self._add_label(
            metadata_layout,
            f"SN：{self._compact_sn(snapshot.sn)}",
            "result_history_card_sn",
            "序列号",
        )
        sn_label.setWordWrap(False)
        sn_label.setToolTip(snapshot.sn or "—")
        trigger_label = self._add_label(
            metadata_layout,
            f"触发：{self._trigger_display(snapshot.trigger_source)}",
            "result_history_card_trigger_source",
            "触发来源",
        )
        trigger_label.setWordWrap(False)
        trigger_label.setToolTip(snapshot.trigger_source or "—")
        metadata_layout.addStretch(1)
        layout.addLayout(metadata_layout)
        self._add_summary(layout, payload)
        self._manual_judgment_slot = QWidget(self)
        self._manual_judgment_slot.setObjectName(
            "result_history_card_action_slot"
        )
        self._manual_judgment_slot_layout = QVBoxLayout(
            self._manual_judgment_slot
        )
        self._manual_judgment_slot_layout.setContentsMargins(0, 0, 0, 0)
        self._manual_judgment_slot_layout.setSpacing(0)
        self._manual_judgment_slot.setFixedHeight(0)
        self._manual_judgment_slot.hide()
        self._manual_judgment_slot.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self._manual_judgment_slot)

    def _add_summary(
        self,
        layout: QVBoxLayout,
        payload: object | None,
    ) -> None:
        summary = self._review_summary(payload)
        overall = self._overall_text(summary)
        status_container = QWidget(self)
        status_layout = QVBoxLayout(status_container)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(0)
        caption = self._add_label(
            status_layout,
            "结果摘要",
            "result_history_card_summary_caption",
            "结果摘要",
        )
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge = _FittedStatusLabel()
        badge.setTextFormat(Qt.TextFormat.PlainText)
        badge.setObjectName("result_history_card_summary_overall")
        badge.setAccessibleName("结果摘要总结果")
        badge.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        status_layout.addWidget(badge)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setWordWrap(False)
        badge_size_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        badge.setSizePolicy(badge_size_policy)
        badge.setFixedHeight(48)
        self._set_overall_display(
            badge,
            overall,
            self._overall_color(summary),
        )
        layout.addWidget(status_container)

        metric_layout = QHBoxLayout()
        metric_layout.setContentsMargins(0, 0, 0, 0)
        metric_layout.setSpacing(8)
        terminal_metrics = self._snapshot.phase in {
            ResultPhase.TERMINAL,
            ResultPhase.REANALYZING,
        }
        spl_text = self._average_display(summary, "spl_average")
        loudness_text = self._average_display(summary, "loudness_average")
        self._spl_metric = self._add_metric(
            metric_layout,
            "平均声压级" if terminal_metrics else "声压级",
            spl_text if terminal_metrics else "--",
            "result_history_card_metric_spl",
        )
        self._loudness_metric = self._add_metric(
            metric_layout,
            "平均响度" if terminal_metrics else "响度",
            loudness_text if terminal_metrics else "--",
            "result_history_card_metric_loudness",
        )
        layout.addLayout(metric_layout)

        raw_rows = summary.get("rows") if summary is not None else None
        if not isinstance(raw_rows, list):
            raw_rows = []
        rows_by_key = {
            row.get("item_key"): row
            for row in raw_rows
            if isinstance(row, dict)
            and isinstance(row.get("item_key"), str)
        }

        table = _FittedSummaryTable(len(_SUMMARY_ROWS), 4, self)
        table.setObjectName("result_history_card_summary_table")
        table.setHorizontalHeaderLabels(
            ["项目", "实测值", "电流", "结果"]
        )
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setWordWrap(True)
        table.setStyleSheet(
            "QTableWidget { font-size: 12px; }"
            "QHeaderView::section { padding: 1px; font-size: 12px; }"
        )
        table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        table.horizontalHeader().setFixedHeight(22)
        table.verticalHeader().setDefaultSectionSize(
            _FittedSummaryTable._ROW_HEIGHT
        )
        table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        header = table.horizontalHeader()
        header.setMinimumSectionSize(45)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        table.setColumnWidth(0, 76)
        table.setColumnWidth(1, 70)
        table.setColumnWidth(2, 54)
        for row_index, (item_key, label) in enumerate(_SUMMARY_ROWS):
            row = rows_by_key.get(item_key, {})
            values = (
                label,
                self._summary_value(row),
                self._current_display(row),
                self._summary_result_text(
                    row.get("result_display") or row.get("status") or "-",
                    preserve_details=item_key == "ai",
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter
                    | (
                        Qt.AlignmentFlag.AlignLeft
                        if column == 0
                        else Qt.AlignmentFlag.AlignCenter
                    )
                )
                if column == 3:
                    normalized = str(row.get("status") or "").lower()
                    if normalized in {"pass", "ok"}:
                        item.setForeground(QColor("#188038"))
                    elif normalized in {"fail", "ng", "no_result"}:
                        item.setForeground(QColor("#d93025"))
                table.setItem(row_index, column, item)
        layout.addWidget(table)
        table.schedule_initial_fit()

    @staticmethod
    def _current_display(row: dict) -> str:
        for key in ("current_display", "channel_label"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            text = value.strip()
            if re.fullmatch(r"CH\d+", text, flags=re.IGNORECASE):
                return "-"
            return ResultHistoryCard._format_numeric_display(text)
        return "-"

    @property
    def manual_judgment_widget(self) -> QWidget | None:
        return self._manual_judgment_widget

    @property
    def manual_judgment_slot(self) -> QWidget:
        return self._manual_judgment_slot

    def set_manual_judgment_widget(
        self,
        widget: QWidget | None,
        *,
        animate: bool = False,
    ) -> None:
        if widget is not None and not isinstance(widget, QWidget):
            raise TypeError("manual judgment widget must be a QWidget or None")
        if (
            widget is self._manual_judgment_widget
            and (
                widget is None
                or widget.parentWidget() is self._manual_judgment_slot
            )
        ):
            return

        previous = self._manual_judgment_widget
        if previous is not None:
            previous.removeEventFilter(self)
            self._manual_judgment_slot_layout.removeWidget(previous)
            previous.hide()
        self._manual_judgment_widget = widget
        if widget is None:
            self._animate_manual_action_height(0, animate=animate)
            return

        widget.setParent(self._manual_judgment_slot)
        self._manual_judgment_slot_layout.addWidget(widget)
        widget.installEventFilter(self)
        widget.setEnabled(True)
        widget.show()
        widget.ensurePolished()
        widget_layout = widget.layout()
        if widget_layout is not None:
            widget_layout.activate()
        self._manual_judgment_slot.show()
        self._animate_manual_action_height(
            self._manual_action_target_height(),
            animate=animate,
        )

    def _manual_action_target_height(self) -> int:
        widget = self._manual_judgment_widget
        if widget is None:
            return 0
        widget_layout = widget.layout()
        if widget_layout is not None:
            widget_layout.activate()
        return max(
            1,
            widget.minimumHeight(),
            widget.sizeHint().height(),
            widget.minimumSizeHint().height(),
        )

    def _sync_manual_action_height(self) -> None:
        if self._manual_judgment_widget is None:
            return
        animation = self._manual_action_animation
        if (
            animation is not None
            and animation.state() is QPropertyAnimation.State.Running
        ):
            return
        target = self._manual_action_target_height()
        if self._manual_judgment_slot.height() != target:
            self._manual_judgment_slot.setFixedHeight(target)

    def _animate_manual_action_height(
        self,
        target_height: int,
        *,
        animate: bool,
    ) -> None:
        animation = self._manual_action_animation
        if animation is not None:
            animation.stop()
            animation.deleteLater()
            self._manual_action_animation = None

        target_height = max(0, int(target_height))
        start_height = self._manual_judgment_slot.height()
        if not animate or start_height == target_height:
            self._finish_manual_action_animation(target_height)
            return

        self._manual_judgment_slot.show()
        self._manual_judgment_slot.setMinimumHeight(0)
        animation = QPropertyAnimation(
            self._manual_judgment_slot,
            b"maximumHeight",
            self,
        )
        animation.setDuration(140)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(start_height)
        animation.setEndValue(target_height)
        animation.finished.connect(
            lambda height=target_height: (
                self._finish_manual_action_animation(height)
            )
        )
        self._manual_action_animation = animation
        animation.start()

    def _finish_manual_action_animation(self, target_height: int) -> None:
        self._manual_action_animation = None
        self._manual_judgment_slot.setFixedHeight(target_height)
        if target_height == 0:
            self._manual_judgment_slot.hide()

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self._manual_judgment_widget
            and event.type() is QEvent.Type.LayoutRequest
        ):
            self._sync_manual_action_height()
        return super().eventFilter(watched, event)

    def can_update_snapshot(
        self,
        snapshot: ResultHistoryCardSnapshot,
    ) -> bool:
        current = self._snapshot
        return (
            current.run_id == snapshot.run_id
            and current.config == snapshot.config
            and current.sn == snapshot.sn
            and current.trigger_source == snapshot.trigger_source
            and current.started_at == snapshot.started_at
        )

    def update_snapshot(
        self,
        snapshot: ResultHistoryCardSnapshot,
        payload: object | None,
    ) -> None:
        if not self.can_update_snapshot(snapshot):
            raise ValueError("snapshot cannot update this result card")
        previous_state = _result_state(self._snapshot)
        self._snapshot = snapshot
        result_state = _result_state(snapshot)
        self.setAccessibleDescription(
            f"结果状态：{_RESULT_STATE_ACCESSIBLE_TEXT[result_state]}"
        )
        self._update_summary(payload)
        if result_state != previous_state:
            self.setProperty("resultState", result_state)
            self._refresh_style()

    def can_update_manual_judgment(
        self,
        snapshot: ResultHistoryCardSnapshot,
    ) -> bool:
        current = self._snapshot
        if (
            current.run_id != snapshot.run_id
            or current.phase is not ResultPhase.TERMINAL
            or snapshot.phase is not ResultPhase.TERMINAL
        ):
            return False
        unchanged_frozen_fields = (
            "config",
            "sn",
            "trigger_source",
            "started_at",
            "terminated_at",
            "is_active_run",
            "manual_judgment_allowed",
            "manual_reanalysis_allowed",
            "operation_id",
        )
        if any(
            getattr(current, field) != getattr(snapshot, field)
            for field in unchanged_frozen_fields
        ):
            return False
        return (
            current.conclusion != snapshot.conclusion
            or current.fault_type != snapshot.fault_type
            or current.manual_judgment_revision
            != snapshot.manual_judgment_revision
        )

    def update_manual_judgment(
        self,
        snapshot: ResultHistoryCardSnapshot,
        payload: object | None,
    ) -> None:
        if not self.can_update_manual_judgment(snapshot):
            raise ValueError("snapshot is not an in-place manual judgment update")
        self.update_snapshot(snapshot, payload)

    def preview_manual_judgment(
        self,
        label: str,
        fault_type: str | None,
    ) -> None:
        """Immediately show an accepted operator intent while it is saved."""

        if self._snapshot.phase is not ResultPhase.TERMINAL:
            return
        if label == "OK":
            conclusion = ResultConclusion.OK
            fault_type = None
        elif label == "NG":
            conclusion = ResultConclusion.NG
        else:
            return

        self._snapshot = replace(
            self._snapshot,
            conclusion=conclusion,
            fault_type=fault_type,
        )
        overall = _CONCLUSION_TEXT[conclusion]
        if conclusion is ResultConclusion.NG and fault_type:
            overall = f"{overall}：{fault_type}"
        badge = self.findChild(
            QLabel,
            "result_history_card_summary_overall",
        )
        self._set_overall_display(
            badge,
            overall,
            "#188038" if conclusion is ResultConclusion.OK else "#d93025",
        )

        result_state = _CONCLUSION_STATE[conclusion]
        self.setAccessibleDescription(
            f"结果状态：{_RESULT_STATE_ACCESSIBLE_TEXT[result_state]}"
        )
        if self.property("resultState") != result_state:
            self.setProperty("resultState", result_state)
            self._refresh_style()

    def _update_summary(self, payload: object | None) -> None:
        summary = self._review_summary(payload)
        badge = self.findChild(
            QLabel,
            "result_history_card_summary_overall",
        )
        self._set_overall_display(
            badge,
            self._overall_text(summary),
            self._overall_color(summary),
        )
        self._update_summary_rows(summary)

    def refresh_measurement_summary(self, payload: object | None) -> None:
        """Refresh payload-backed rows without changing card judgment state."""

        self._update_summary_rows(self._review_summary(payload))

    def _update_summary_rows(self, summary: dict | None) -> None:
        raw_rows = summary.get("rows") if summary is not None else None
        if not isinstance(raw_rows, list):
            raw_rows = []
        rows_by_key = {
            row.get("item_key"): row
            for row in raw_rows
            if isinstance(row, dict)
            and isinstance(row.get("item_key"), str)
        }
        table = self.findChild(
            QTableWidget,
            "result_history_card_summary_table",
        )
        for row_index, (item_key, label) in enumerate(_SUMMARY_ROWS):
            row = rows_by_key.get(item_key, {})
            values = (
                label,
                self._summary_value(row),
                self._current_display(row),
                self._summary_result_text(
                    row.get("result_display") or row.get("status") or "-",
                    preserve_details=item_key == "ai",
                ),
            )
            for column, value in enumerate(values):
                item = table.item(row_index, column)
                item.setText(str(value))
                item.setToolTip(str(value))
                if column != 3:
                    continue
                normalized = str(row.get("status") or "").lower()
                if normalized in {"pass", "ok"}:
                    item.setForeground(QColor("#188038"))
                elif normalized in {"fail", "ng", "no_result"}:
                    item.setForeground(QColor("#d93025"))
                else:
                    item.setData(Qt.ItemDataRole.ForegroundRole, None)
        table.schedule_initial_fit()

    @staticmethod
    def _review_summary(payload: object | None) -> dict | None:
        if type(payload) is not ReviewDisplayPayload:
            return None
        review = payload.review
        summary = (
            review.get("summary_payload")
            if isinstance(review, dict)
            else getattr(review, "summary_payload", None)
        )
        return summary if isinstance(summary, dict) else None

    def _overall_text(self, summary: dict | None) -> str:
        if self._snapshot.phase is ResultPhase.REANALYZING:
            return "重新分析中"
        if self._snapshot.phase is not ResultPhase.TERMINAL:
            return "进行中"
        raw = summary.get("overall_label") if summary is not None else None
        overall = (
            _SUMMARY_RESULT_TEXT.get(str(raw), str(raw))
            if isinstance(raw, str) and raw.strip()
            else _CONCLUSION_TEXT.get(self._snapshot.conclusion, "-")
        )
        if (
            self._snapshot.conclusion is ResultConclusion.NG
            and self._snapshot.fault_type
        ):
            return (
                f"{_CONCLUSION_TEXT[ResultConclusion.NG]}："
                f"{self._snapshot.fault_type}"
            )
        return overall

    def _overall_color(self, summary: dict | None) -> str:
        if self._snapshot.phase is not ResultPhase.TERMINAL:
            return "#424242"
        status = summary.get("overall_status") if summary is not None else None
        if status == "pass" or (
            status is None
            and self._snapshot.conclusion is ResultConclusion.OK
        ):
            return "#188038"
        if status in {"fail", "no_result"} or (
            status is None
            and self._snapshot.conclusion
            in {
                ResultConclusion.NG,
                ResultConclusion.NO_RESULT,
                ResultConclusion.ERROR,
            }
        ):
            return "#d93025"
        return "#424242"

    @staticmethod
    def _set_overall_display(
        label: _FittedStatusLabel,
        text: str,
        color: str,
    ) -> None:
        label.set_display(text, color)

    @classmethod
    def _average_display(cls, summary: dict | None, item_key: str) -> str:
        if summary is None or not isinstance(summary.get("rows"), list):
            return "--"
        for row in summary["rows"]:
            if isinstance(row, dict) and row.get("item_key") == item_key:
                display = row.get("actual_display")
                if cls._valid_display(display):
                    return cls._format_numeric_display(display.strip())
                return "--"
        return "--"

    @staticmethod
    def _valid_display(value: object) -> bool:
        return isinstance(value, str) and value.strip() not in {"", "-", "--"}

    def _add_metric(
        self,
        layout: QHBoxLayout,
        label_text: str,
        value_text: str,
        object_name: str,
    ) -> QLabel:
        panel = QFrame(self)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(6, 4, 6, 4)
        panel_layout.setSpacing(1)
        label = self._add_label(
            panel_layout,
            label_text,
            f"{object_name}_label",
            label_text,
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value = self._add_label(
            panel_layout,
            value_text,
            object_name,
            f"{label_text}数值",
        )
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value.setStyleSheet("font-weight: 600;")
        layout.addWidget(panel, 1)
        return value

    def update_metric_view(
        self,
        *,
        spl_label: str,
        spl_text: str,
        loudness_label: str,
        loudness_text: str,
    ) -> None:
        self.findChild(
            QLabel, "result_history_card_metric_spl_label"
        ).setText(spl_label)
        self._spl_metric.setText(spl_text)
        self.findChild(
            QLabel, "result_history_card_metric_loudness_label"
        ).setText(loudness_label)
        self._loudness_metric.setText(loudness_text)

    @classmethod
    def _summary_value(cls, row: dict) -> str:
        display = row.get("actual_display")
        if display is not None:
            return cls._format_numeric_display(display)
        value = row.get("actual_value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{float(value):.2f}"
        return "-"

    @staticmethod
    def _format_numeric_display(value: object) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{float(value):.2f}"
        text = str(value).strip()
        match = re.fullmatch(
            r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*)",
            text,
        )
        if match is None:
            return text
        suffix = match.group(2).strip()
        number = f"{float(match.group(1)):.2f}"
        return f"{number} {suffix}".rstrip()

    @staticmethod
    def _compact_sn(sn: str) -> str:
        text = sn.strip()
        if not text:
            return "—"
        if len(text) <= 15:
            return text
        return f"{text[:6]}...{text[-6:]}"

    @staticmethod
    def _trigger_display(trigger_source: str) -> str:
        text = trigger_source.strip()
        return {
            "plc": "PLC",
            "manual": "手动",
            "scan": "扫码",
            "scanner": "扫码",
            "import": "导入",
            "import_audio": "导入",
        }.get(text.lower(), text or "—")

    @staticmethod
    def _summary_result_text(
        value: object,
        *,
        preserve_details: bool = False,
    ) -> str:
        text = str(value).strip()
        mapped = _SUMMARY_RESULT_TEXT.get(text.upper())
        if mapped is not None:
            return mapped
        if preserve_details and text:
            return text
        if "不通过" in text:
            return "不通过"
        if "通过" in text:
            return "通过"
        return text

    @staticmethod
    def _add_label(
        layout: QVBoxLayout | QHBoxLayout,
        text: str,
        object_name: str,
        accessible_name: str,
    ) -> QLabel:
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setText(text)
        label.setObjectName(object_name)
        label.setAccessibleName(accessible_name)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(label)
        return label

    def set_selected(self, selected: bool) -> None:
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("selected", self._selected)
        self._refresh_style()

    def set_selection_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._selection_enabled:
            return
        self._selection_enabled = enabled
        self.setProperty("selectionEnabled", self._selection_enabled)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if self._selection_enabled
            else Qt.CursorShape.ArrowCursor
        )
        self.setToolTip(
            "点击查看此结果"
            if self._selection_enabled
            else "结果跟随已锁定，当前卡片不可选择"
        )
        self._refresh_style()

    def _refresh_style(self) -> None:
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def _activate(self) -> bool:
        if not self._selection_enabled:
            return False
        self.activated.emit(self._run_id)
        return True

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() is Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            if self._activate():
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self._activate()
            event.accept()
            return
        super().keyPressEvent(event)
