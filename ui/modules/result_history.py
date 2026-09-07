from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.service.result_history import (
    ResultPhase,
    ReviewDisplayPayload,
    ResultHistoryCardSnapshot,
    ResultHistorySnapshot,
    ResultHistoryTransition,
)
from app.service.analysis import dsp
from ui.widgets.result_history_card import ResultHistoryCard


@dataclass
class _MetricViewState:
    config_id: int
    primary_channel: int | None
    loudness_unit: str | None
    spl_db: float | None = None
    loudness_sone: float | None = None
    terminal_sealed: bool = False


class ResultHistoryModule(QWidget):
    """Fixed history column that projects immutable result-history values."""

    run_selected = pyqtSignal(str)
    follow_latest_requested = pyqtSignal(bool)
    decisionPainted = pyqtSignal(object, object)

    def __init__(
        self,
        parent=None,
        *,
        payload_provider: Callable[[str], object | None] | None = None,
        config_provider: Callable[[int], dict | None] | None = None,
        manual_judgment_widget: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._payload_provider = payload_provider
        self._config_provider = config_provider
        self._metric_view_state_by_run: dict[str, _MetricViewState] = {}
        self._follow_latest = True
        self._cards: list[ResultHistoryCard] = []
        self._card_entries: dict[
            str,
            tuple[ResultHistoryCardSnapshot, ResultHistoryCard],
        ] = {}
        self._rendered_snapshot: ResultHistorySnapshot | None = None
        self._manual_judgment_widget = manual_judgment_widget
        self._manual_judgment_card: ResultHistoryCard | None = None
        self._pending_paint_decision: tuple[str, str, str, int] | None = None
        self._paint_ack_scheduled = False
        self._last_painted_decision_by_run: dict[
            str, tuple[str, str, str, int]
        ] = {}
        self._paint_ack_timer = QTimer(self)
        self._paint_ack_timer.setSingleShot(True)
        self._paint_ack_timer.timeout.connect(
            self._finish_pending_decision_paint
        )

        self.setObjectName("result_history_module")
        self.setAccessibleName("结果历史摘要")
        self.setFixedWidth(360)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._notice_label = QLabel()
        self._notice_label.setObjectName("result_history_notice")
        self._notice_label.setAccessibleName("结果历史提示")
        self._notice_label.setWordWrap(True)
        self._notice_label.hide()
        root.addWidget(self._notice_label)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("result_history_scroll")
        self._scroll.setAccessibleName("结果历史卡片列表")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll.setEnabled(True)
        root.addWidget(self._scroll, 1)

        self._scroll_content = QWidget()
        self._scroll_content.setObjectName("result_history_scroll_content")
        content_layout = QVBoxLayout(self._scroll_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.setSizeConstraint(
            QLayout.SizeConstraint.SetMinimumSize
        )

        self._active_section, self._active_cards_layout = (
            self._create_section(
                "result_history_active_section",
                None,
            )
        )
        content_layout.addWidget(self._active_section)

        self._terminal_section, self._terminal_cards_layout = (
            self._create_section(
                "result_history_terminal_section",
                "历史结果",
            )
        )
        self._terminal_empty = QLabel("暂无结果")
        self._terminal_empty.setObjectName("result_history_terminal_empty")
        self._terminal_empty.setAccessibleName("暂无历史结果")
        self._terminal_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._terminal_cards_layout.addWidget(self._terminal_empty)
        content_layout.addWidget(self._terminal_section)
        content_layout.addStretch(1)
        self._scroll.setWidget(self._scroll_content)
        if manual_judgment_widget is not None:
            manual_judgment_widget.hide()
            manual_judgment_widget.setEnabled(False)
            manual_judgment_widget.setParent(self)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addStretch(1)
        self._lock_button = QToolButton()
        self._lock_button.setObjectName(
            "result_history_follow_latest_button"
        )
        self._lock_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self._lock_button.clicked.connect(self._toggle_follow_latest)
        controls.addWidget(self._lock_button)
        root.addLayout(controls)

        self._update_follow_latest_presentation()

    @staticmethod
    def _create_section(
        object_name: str,
        title: str | None,
    ) -> tuple[QWidget, QVBoxLayout]:
        section = QWidget()
        section.setObjectName(object_name)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        if title is not None:
            heading = QLabel(title)
            heading.setObjectName(f"{object_name}_heading")
            heading.setAccessibleName(f"{title}区域标题")
            layout.addWidget(heading)
        return section, layout

    def render_snapshot(self, snapshot: ResultHistorySnapshot) -> None:
        self.setUpdatesEnabled(False)
        try:
            self._render_snapshot_now(snapshot)
        finally:
            self.setUpdatesEnabled(True)
            self.update()

    def refresh_terminal_payload(
        self,
        run_id: str,
        payload: ReviewDisplayPayload,
    ) -> bool:
        entry = self._card_entries.get(run_id)
        if (
            entry is None
            or entry[0].is_active_run
            or entry[0].phase is not ResultPhase.TERMINAL
        ):
            return False
        snapshot, card = entry
        card.refresh_measurement_summary(payload)
        self._replay_metric_view(snapshot, card, payload)
        card.update()
        return True

    def project_decision_revision(
        self,
        run_id: str,
        attempt_id: str,
        terminal_revision: str,
        history_revision: int,
    ) -> bool:
        decision = (
            run_id,
            attempt_id,
            terminal_revision,
            history_revision,
        )
        if (
            not self._decision_projection_is_current(decision)
            or decision == self._pending_paint_decision
            or decision == self._last_painted_decision_by_run.get(run_id)
        ):
            return False
        entry = self._card_entries[run_id]
        entry[1].installEventFilter(self)
        self._pending_paint_decision = decision
        entry[1].update()
        return True

    def eventFilter(self, watched, event) -> bool:
        decision = self._pending_paint_decision
        if (
            decision is not None
            and event.type() is QEvent.Type.Paint
            and self._card_entries.get(decision[0], (None, None))[1]
            is watched
            and not self._paint_ack_scheduled
        ):
            self._paint_ack_scheduled = True
            self._paint_ack_timer.setProperty("expectedDecision", decision)
            self._paint_ack_timer.start(0)
        return super().eventFilter(watched, event)

    def _finish_pending_decision_paint(self) -> None:
        self._paint_ack_scheduled = False
        expected = self._paint_ack_timer.property("expectedDecision")
        if (
            type(expected) is not tuple
            or self._pending_paint_decision != expected
            or not self._decision_projection_is_current(expected)
        ):
            return
        self._pending_paint_decision = None
        self._last_painted_decision_by_run[expected[0]] = expected
        self.decisionPainted.emit(expected, time.perf_counter_ns())

    def _decision_projection_is_current(
        self,
        decision: tuple[str, str, str, int],
    ) -> bool:
        run_id, attempt_id, terminal_revision, history_revision = decision
        snapshot = self._rendered_snapshot
        entry = self._card_entries.get(run_id)
        if (
            snapshot is None
            or snapshot.selected_run_id != run_id
            or entry is None
            or entry[0].revision != history_revision
            or entry[0].phase is not ResultPhase.TERMINAL
        ):
            return False
        payload = (
            self._payload_provider(run_id)
            if self._payload_provider is not None
            else None
        )
        if type(payload) is not ReviewDisplayPayload:
            return False
        review = payload.review
        identity = (
            review.get("_projection_identity")
            if type(review) is dict
            else None
        )
        return (
            type(identity) is dict
            and identity.get("attempt_id") == attempt_id
            and identity.get("terminal_revision") == terminal_revision
            and identity.get("history_revision") == history_revision
        )

    def preview_manual_judgment(
        self,
        run_id: str,
        label: str,
        fault_type: str | None,
    ) -> bool:
        entry = self._card_entries.get(run_id)
        if entry is None:
            return False
        entry[1].preview_manual_judgment(label, fault_type)
        return True

    def _render_snapshot_now(self, snapshot: ResultHistorySnapshot) -> None:
        self._reconcile_metric_view_state(snapshot)
        scroll_value = self._scroll.verticalScrollBar().value()
        reveal_selected = self._should_reveal_selected(snapshot)
        self._set_notice(None)
        self._follow_latest = snapshot.follow_latest
        self._update_follow_latest_presentation()
        previous_entries = self._card_entries
        selected_snapshot = next(
            (
                card
                for card in (*snapshot.active_cards, *snapshot.terminal_cards)
                if card.run_id == snapshot.selected_run_id
            ),
            None,
        )
        legal_selected = (
            selected_snapshot is not None
            and not selected_snapshot.is_active_run
            and selected_snapshot.manual_judgment_allowed
            and selected_snapshot.config.manual_judgment_buttons_visible
        )
        next_entries: dict[
            str,
            tuple[ResultHistoryCardSnapshot, ResultHistoryCard],
        ] = {}
        next_cards: list[ResultHistoryCard] = []

        zones = (
            (
                snapshot.active_cards,
                self._active_section,
                self._active_cards_layout,
                0,
            ),
            (
                snapshot.terminal_cards,
                self._terminal_section,
                self._terminal_cards_layout,
                1,
            ),
        )
        for card_snapshots, section, layout, first_index in zones:
            for index, card_snapshot in enumerate(card_snapshots):
                previous = previous_entries.get(card_snapshot.run_id)
                payload = (
                    self._payload_provider(card_snapshot.run_id)
                    if self._payload_provider is not None
                    and not card_snapshot.is_active_run
                    else None
                )
                if previous is not None and previous[0] == card_snapshot:
                    card = previous[1]
                elif (
                    previous is not None
                    and previous[1].can_update_snapshot(card_snapshot)
                ):
                    card = previous[1]
                    card.update_snapshot(card_snapshot, payload)
                else:
                    if previous is not None:
                        self._retire_card(previous[1])
                    card = ResultHistoryCard(
                        card_snapshot,
                        section,
                        payload=payload,
                    )
                    card.activated.connect(self.run_selected)
                self._place_card(
                    card, section, layout, index + first_index
                )
                card.set_selected(
                    card_snapshot.run_id == snapshot.selected_run_id
                )
                card.set_selection_enabled(True)
                self._replay_metric_view(card_snapshot, card, payload)
                card.show()
                next_entries[card_snapshot.run_id] = (
                    card_snapshot,
                    card,
                )
                next_cards.append(card)

        for run_id, (_, card) in previous_entries.items():
            if run_id not in next_entries:
                self._retire_card(card)

        self._card_entries = next_entries
        self._cards = next_cards
        manual_judgment_card = None
        if (
            legal_selected
            and snapshot.selected_run_id is not None
            and self._manual_judgment_widget is not None
        ):
            selected_entry = next_entries.get(snapshot.selected_run_id)
            if selected_entry is not None:
                manual_judgment_card = selected_entry[1]
        self._set_manual_judgment_card(manual_judgment_card)
        self._terminal_empty.setVisible(not snapshot.terminal_cards)
        self._rendered_snapshot = snapshot
        self._discard_stale_decision_paint_state()
        self._restore_scroll_value(scroll_value)
        if reveal_selected and snapshot.selected_run_id is not None:
            QTimer.singleShot(
                0,
                lambda run_id=snapshot.selected_run_id: self._ensure_run_visible(
                    run_id
                ),
            )

    def _discard_stale_decision_paint_state(self) -> None:
        pending = self._pending_paint_decision
        if pending is not None and not self._decision_projection_is_current(
            pending
        ):
            self._paint_ack_timer.stop()
            self._paint_ack_scheduled = False
            self._pending_paint_decision = None
        retained_run_ids = self._card_entries.keys()
        self._last_painted_decision_by_run = {
            run_id: decision
            for run_id, decision in self._last_painted_decision_by_run.items()
            if run_id in retained_run_ids
        }

    def _set_manual_judgment_card(
        self,
        card: ResultHistoryCard | None,
    ) -> None:
        widget = self._manual_judgment_widget
        if widget is None:
            self._manual_judgment_card = None
            return
        previous = self._manual_judgment_card
        if (
            previous is card
            and card is not None
            and card.manual_judgment_widget is widget
        ):
            widget.setEnabled(True)
            widget.show()
            return
        if previous is not None:
            previous.set_manual_judgment_widget(
                None,
                animate=card is not None,
            )
        widget.hide()
        widget.setParent(self)
        self._manual_judgment_card = card
        if card is None:
            widget.setEnabled(False)
            return
        card.set_manual_judgment_widget(widget, animate=True)
        widget.setEnabled(True)
        widget.show()

    def _ensure_run_visible(self, run_id: str) -> None:
        snapshot = self._rendered_snapshot
        if snapshot is None or snapshot.selected_run_id != run_id:
            return
        entry = self._card_entries.get(run_id)
        if entry is not None:
            self._scroll.ensureWidgetVisible(entry[1], 0, 0)

    def _reconcile_metric_view_state(
        self,
        snapshot: ResultHistorySnapshot,
    ) -> None:
        visible = {
            card.run_id: card
            for card in (*snapshot.active_cards, *snapshot.terminal_cards)
        }
        for run_id in tuple(self._metric_view_state_by_run):
            if run_id not in visible:
                del self._metric_view_state_by_run[run_id]
        for run_id, card in visible.items():
            state = self._metric_view_state_by_run.get(run_id)
            if state is None:
                state = self._create_metric_view_state(card)
                self._metric_view_state_by_run[run_id] = state
            if not card.is_active_run:
                state.terminal_sealed = True

    def _create_metric_view_state(
        self,
        card: ResultHistoryCardSnapshot,
    ) -> _MetricViewState:
        config_id = card.config.config_id
        config = None
        if card.is_active_run and self._config_provider is not None:
            try:
                candidate = self._config_provider(config_id)
                if isinstance(candidate, dict):
                    config = candidate
            except Exception:
                config = None
        primary = None
        unit = None
        if config is not None:
            selected = self._valid_channels(config.get("selected_channels"))
            vibration = self._as_channel(
                config.get("vibration_view_channel_index")
            )
            primary = next(
                (channel for channel in selected if channel != vibration),
                None,
            )
            if primary is not None:
                unit = self._channel_loudness_unit(config, primary)
        return _MetricViewState(config_id, primary, unit)

    @classmethod
    def _valid_channels(cls, values: object) -> tuple[int, ...]:
        if not isinstance(values, (list, tuple)):
            return ()
        result = []
        for value in values:
            channel = cls._as_channel(value)
            if channel is not None and channel not in result:
                result.append(channel)
        return tuple(result)

    @staticmethod
    def _as_channel(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            channel = int(value)
        except (TypeError, ValueError):
            return None
        return channel if channel >= 0 else None

    @classmethod
    def _channel_loudness_unit(
        cls,
        config: dict,
        primary_channel: int,
    ) -> str:
        channels = config.get("channels")
        if isinstance(channels, list):
            for channel in channels:
                if (
                    isinstance(channel, dict)
                    and cls._as_channel(channel.get("channel_index"))
                    == primary_channel
                ):
                    value = str(
                        channel.get("loudness_unit") or "sone"
                    ).strip().lower()
                    return value if value in {"sone", "phon"} else "sone"
        return "sone"

    def _replay_metric_view(
        self,
        snapshot: ResultHistoryCardSnapshot,
        card: ResultHistoryCard,
        payload: object | None,
    ) -> None:
        state = self._metric_view_state_by_run[snapshot.run_id]
        if snapshot.phase not in {
            ResultPhase.TERMINAL,
            ResultPhase.REANALYZING,
        }:
            card.update_metric_view(
                spl_label="声压级",
                spl_text=self._format_spl(state.spl_db),
                loudness_label="响度",
                loudness_text=self._format_loudness(
                    state.loudness_sone, state.loudness_unit
                ),
            )
            return
        summary = ResultHistoryCard._review_summary(payload)
        spl = ResultHistoryCard._average_display(summary, "spl_average")
        loudness = ResultHistoryCard._average_display(
            summary, "loudness_average"
        )
        card.update_metric_view(
            spl_label="平均声压级",
            spl_text=(
                spl if spl != "--" else self._format_spl(state.spl_db)
            ),
            loudness_label="平均响度",
            loudness_text=(
                loudness
                if loudness != "--"
                else self._format_loudness(
                    state.loudness_sone, state.loudness_unit
                )
            ),
        )

    @pyqtSlot(int, str, int, object)
    def on_live_spl(
        self,
        config_id: int,
        run_id: str,
        channel_idx: int,
        frame: object,
    ) -> None:
        state = self._live_state(config_id, run_id, channel_idx)
        if state is None:
            return
        value = self._finite_value(getattr(frame, "spl_db", None))
        if value is None:
            return
        state.spl_db = value
        self._refresh_live_card(run_id, state)

    @pyqtSlot(int, str, int, object)
    def on_live_loudness(
        self,
        config_id: int,
        run_id: str,
        channel_idx: int,
        frame: object,
    ) -> None:
        state = self._live_state(config_id, run_id, channel_idx)
        if state is None:
            return
        value = self._finite_value(
            getattr(frame, "loudness_sone", None)
        )
        if value is None:
            return
        state.loudness_sone = value
        self._refresh_live_card(run_id, state)

    def _live_state(
        self,
        config_id: int,
        run_id: str,
        channel_idx: int,
    ) -> _MetricViewState | None:
        state = self._metric_view_state_by_run.get(run_id)
        if (
            state is None
            or state.terminal_sealed
            or state.config_id != config_id
            or state.primary_channel != channel_idx
        ):
            return None
        return state

    def _refresh_live_card(
        self,
        run_id: str,
        state: _MetricViewState,
    ) -> None:
        entry = self._card_entries.get(run_id)
        if entry is None or not entry[0].is_active_run:
            return
        entry[1].update_metric_view(
            spl_label="声压级",
            spl_text=self._format_spl(state.spl_db),
            loudness_label="响度",
            loudness_text=self._format_loudness(
                state.loudness_sone, state.loudness_unit
            ),
        )

    @staticmethod
    def _finite_value(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if isfinite(number) else None

    @staticmethod
    def _format_spl(value: float | None) -> str:
        return "--" if value is None else f"{value:.1f} dB"

    @staticmethod
    def _format_loudness(
        value: float | None,
        unit: str | None,
    ) -> str:
        if value is None or unit is None:
            return "--"
        if unit == "phon":
            try:
                phon = float(dsp.sone_to_phon(value))
            except (TypeError, ValueError, OverflowError):
                return "--"
            return f"{phon:.1f} phon" if isfinite(phon) else "--"
        return (
            f"{value:.2f} sone"
            if value < 10.0
            else f"{value:.1f} sone"
        )

    def render_transition(
        self,
        transition: ResultHistoryTransition,
    ) -> None:
        self.render_snapshot(transition.snapshot)
        self._set_notice(transition.notice)

    @staticmethod
    def _place_card(
        card: ResultHistoryCard,
        section: QWidget,
        layout: QVBoxLayout,
        index: int,
    ) -> None:
        if card.parentWidget() is not section:
            previous_parent = card.parentWidget()
            if previous_parent is not None:
                previous_layout = previous_parent.layout()
                if previous_layout is not None:
                    previous_layout.removeWidget(card)
            card.setParent(section)
        if layout.indexOf(card) != index:
            layout.removeWidget(card)
            layout.insertWidget(index, card)

    def _retire_card(self, card: ResultHistoryCard) -> None:
        if card is self._manual_judgment_card:
            widget = self._manual_judgment_widget
            card.set_manual_judgment_widget(None)
            if widget is not None:
                widget.hide()
                widget.setParent(self)
            self._manual_judgment_card = None
        parent = card.parentWidget()
        if parent is not None:
            layout = parent.layout()
            if layout is not None:
                layout.removeWidget(card)
        card.hide()
        card.deleteLater()

    def _restore_scroll_value(self, value: int) -> None:
        scrollbar = self._scroll.verticalScrollBar()
        scrollbar.setValue(min(value, scrollbar.maximum()))

    def _should_reveal_selected(
        self,
        snapshot: ResultHistorySnapshot,
    ) -> bool:
        previous = self._rendered_snapshot
        if previous is None or not snapshot.follow_latest:
            return False
        selected_run_id = snapshot.selected_run_id
        if selected_run_id is None:
            return False
        if not previous.follow_latest:
            return True
        previous_active = {
            card.run_id for card in previous.active_cards
        }
        previous_terminal = {
            card.run_id for card in previous.terminal_cards
        }
        selected_is_new_active = (
            any(
                card.run_id == selected_run_id
                for card in snapshot.active_cards
            )
            and selected_run_id not in previous_active
        )
        selected_entered_terminal = (
            any(
                card.run_id == selected_run_id
                for card in snapshot.terminal_cards
            )
            and selected_run_id not in previous_terminal
        )
        return selected_is_new_active or selected_entered_terminal

    def _toggle_follow_latest(self) -> None:
        self._follow_latest = not self._follow_latest
        self._update_follow_latest_presentation()
        for card in self._cards:
            card.set_selection_enabled(True)
        self.follow_latest_requested.emit(self._follow_latest)

    def _update_follow_latest_presentation(self) -> None:
        if self._follow_latest:
            self._lock_button.setText("🔒")
            self._lock_button.setAccessibleName(
                "结果历史锁定状态：已锁定"
            )
            self._lock_button.setToolTip(
                "自动跟随：仍可查看和标注历史结果；新结果到达时返回最新"
            )
        else:
            self._lock_button.setText("🔓")
            self._lock_button.setAccessibleName(
                "结果历史锁定状态：已解锁"
            )
            self._lock_button.setToolTip(
                "停留当前：新结果到达时不改变当前选择；点击恢复自动跟随"
            )

    def closeEvent(self, event) -> None:
        self.clear_metric_view_state()
        super().closeEvent(event)

    def clear_metric_view_state(self) -> None:
        self._metric_view_state_by_run.clear()

    def _set_notice(self, notice: str | None) -> None:
        self._notice_label.setText(notice or "")
        self._notice_label.setVisible(bool(notice))
