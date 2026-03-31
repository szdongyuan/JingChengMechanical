from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services_container import FactoryControllerState, FactoryReviewPayload


class ResultModule(QWidget):
    module_id = "result"
    _ROW_LABELS = ("SPL", "Loudness", "FFT", "倍频程", "AI分析")
    _ACTIVE_PHASES = frozenset({"running", "analyzing", "reanalyzing"})

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._factory_state = getattr(services.factory_mode, "state", FactoryControllerState())
        self._review_payload = getattr(services.factory_mode, "review_payload", FactoryReviewPayload())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        top = QWidget()
        top.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        top_lay.addWidget(QLabel("总结果："))

        self.lb_overall = QLabel("-")
        self.lb_overall.setObjectName("ResultBadge")
        self.lb_overall.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_overall.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.lb_overall.setMinimumHeight(36)
        self.lb_overall.setMinimumWidth(180)
        self.lb_overall.setProperty("resultLevel", "unknown")
        top_lay.addStretch(1)
        top_lay.addWidget(self.lb_overall)
        lay.addWidget(top)

        self.empty_state = QWidget()
        self.empty_state.setObjectName("EmptyState")
        self.empty_state.setVisible(False)
        error_lay = QVBoxLayout(self.empty_state)
        error_lay.setContentsMargins(0, 0, 0, 0)
        error_lay.setSpacing(4)

        self.lb_empty_icon = QLabel("!")
        self.lb_empty_icon.setObjectName("EmptyIcon")
        self.lb_empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_empty_icon.setVisible(False)
        error_lay.addWidget(self.lb_empty_icon)

        self.lb_empty_text = QLabel("")
        self.lb_empty_text.setObjectName("EmptyText")
        self.lb_empty_text.setWordWrap(True)
        self.lb_empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_empty_text.setVisible(False)
        error_lay.addWidget(self.lb_empty_text)
        self.error_label = self.lb_empty_text
        lay.addWidget(self.empty_state)

        self.table = QTableWidget(len(self._ROW_LABELS), 4)
        self.table.setHorizontalHeaderLabels(["项目", "偏差值", "通道", "结果"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 80)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.table.setFixedHeight(
            self.table.horizontalHeader().height() + self.table.rowHeight(0) * len(self._ROW_LABELS) + 18
        )
        lay.addWidget(self.table)
        lay.addStretch(1)

        self._connect_bus()
        self._refresh_display()

    def _connect_bus(self) -> None:
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)

    def _set_cell(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        if column == 0:
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        else:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, column, item)

    def _set_row(self, row: int, label: str, deviation: str, channel: str, status: str) -> None:
        self._set_cell(row, 0, label)
        self._set_cell(row, 1, deviation)
        self._set_cell(row, 2, channel)
        self._set_cell(row, 3, status)

    def _set_badge(self, text: str, level: str) -> None:
        self.lb_overall.setText(text)
        self.lb_overall.setProperty("resultLevel", level)
        self.lb_overall.style().unpolish(self.lb_overall)
        self.lb_overall.style().polish(self.lb_overall)

    def _set_error_message(self, message: str | None) -> None:
        visible = bool(message)
        self.empty_state.setVisible(visible)
        self.lb_empty_icon.setVisible(visible)
        self.lb_empty_text.setVisible(visible)
        self.lb_empty_text.setText(message or "")

    def _set_placeholder_rows(self) -> None:
        for row, label in enumerate(self._ROW_LABELS):
            self._set_row(row, label, "-", "-", "-")

    def _show_empty(self) -> None:
        self._set_badge("-", "unknown")
        self._set_error_message(None)
        self._set_placeholder_rows()

    def _show_in_progress(self) -> None:
        self._set_badge("分析中", "unknown")
        self._set_error_message(None)
        self._set_placeholder_rows()

    def _render_summary(self, summary_payload: dict) -> None:
        overall_status = summary_payload.get("overall_status")
        overall_label = self._text_or_default(summary_payload.get("overall_label"), "-")
        badge_level = {
            "pass": "pass",
            "fail": "fail",
            "no_result": "fail",
        }.get(overall_status, "unknown")
        self._set_badge(overall_label, badge_level)
        error_message = None
        if overall_status == "no_result":
            error_message = self._text_or_default(summary_payload.get("error_message"), None)
        self._set_error_message(error_message)

        rows = summary_payload.get("rows")
        if not isinstance(rows, list):
            rows = []
        for row_index, default_label in enumerate(self._ROW_LABELS):
            row_payload = rows[row_index] if row_index < len(rows) and isinstance(rows[row_index], dict) else {}
            self._set_row(
                row_index,
                self._text_or_default(row_payload.get("label"), default_label),
                self._text_or_default(row_payload.get("deviation_display"), "-"),
                self._text_or_default(row_payload.get("channel_label"), "-"),
                self._text_or_default(row_payload.get("status"), "-"),
            )

    def _refresh_display(self) -> None:
        summary_payload = getattr(self._review_payload, "summary_payload", None)
        if isinstance(summary_payload, dict):
            self._render_summary(summary_payload)
            return
        if self._is_in_progress():
            self._show_in_progress()
            return
        self._show_empty()

    def _is_in_progress(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._ACTIVE_PHASES
            and getattr(self._review_payload, "summary_payload", None) is None
        )

    def _on_factory_state_changed(self, state) -> None:
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()
        self._refresh_display()

    def _on_factory_review_changed(self, review) -> None:
        if isinstance(review, FactoryReviewPayload):
            self._review_payload = review
        else:
            self._review_payload = FactoryReviewPayload()
        self._refresh_display()

    def _on_factory_display_reset(self, _reason: str) -> None:
        self._review_payload = FactoryReviewPayload()
        self._show_empty()

    @staticmethod
    def _text_or_default(value, default: str | None) -> str | None:
        if value is None:
            return default
        text = str(value)
        if text == "":
            return default
        return text
