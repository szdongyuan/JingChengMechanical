from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from PyQt6.QtCore import QTimer, pyqtSlot
from PyQt6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget

from app.service.multi_run_coordinator_mvp.contracts import (
    ABSENT,
    HistoryFactKind,
    ProjectedHistoryEvent,
)
from app.service.run_persistence.contracts import Judgment, RunState


_LOCAL_TIMEZONE = timezone(timedelta(hours=8))
_TERMINAL_STATES = frozenset(
    state.value for state in RunState if state is not RunState.ACTIVE
)
_JUDGED_STATES = frozenset({Judgment.PASS.value, Judgment.FAIL.value})
_REFRESH_HISTORY_KINDS = frozenset(
    {
        HistoryFactKind.RUN_TERMINATED,
        HistoryFactKind.TERMINAL_RESULT_REPLACED,
    }
)


class TodayStatisticsModule(QWidget):
    module_id = "today_statistics"

    def __init__(self, services):
        super().__init__()
        self.services = services

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(6)

        self.lb_total = self._create_value_label()
        self.lb_ok = self._create_value_label("#16a34a")
        self.lb_ng = self._create_value_label("#dc2626")
        self.lb_abnormal = self._create_value_label("#eab308")

        # 两行显示：第一行总数，第二行 OK / NG 数。
        layout.addWidget(QLabel("今日总测试数："), 0, 0)
        layout.addWidget(self.lb_total, 0, 1)
        layout.addWidget(QLabel("异常数："), 0, 2)
        layout.addWidget(self.lb_abnormal, 0, 3)
        layout.addWidget(QLabel("OK数："), 1, 0)
        layout.addWidget(self.lb_ok, 1, 1)
        layout.addWidget(QLabel("NG数："), 1, 2)
        layout.addWidget(self.lb_ng, 1, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.services.bus.run_records_deleted.connect(
            self.on_run_records_deleted
        )
        self.refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def _create_value_label(self, color: str = "#2563eb") -> QLabel:
        label = QLabel("0")
        label.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {color};")
        label.setMinimumWidth(44)
        return label

    def on_committed_history_event(self, event: object) -> None:
        """Refresh after committed terminal writes that can change statistics."""

        if (
            type(event) is not ProjectedHistoryEvent
            or event.kind not in _REFRESH_HISTORY_KINDS
            or event.expected_revision is ABSENT
        ):
            return
        rows = tuple(self.services.run_export_reader.list_durable())
        if not any(
            row.run_id == event.run_id and self._is_terminal_row(row)
            for row in rows
        ):
            return
        self._refresh_from_rows(rows)

    def refresh(self) -> None:
        self._refresh_from_rows(
            tuple(self.services.run_export_reader.list_durable())
        )

    @pyqtSlot(object)
    def on_run_records_deleted(self, report: object) -> None:
        results = getattr(report, "results", ())
        if any(
            getattr(result, "database_committed", False) is True
            for result in results
        ):
            self.refresh()

    def _refresh_from_rows(self, rows: tuple[object, ...]) -> None:
        today = datetime.now(_LOCAL_TIMEZONE).date()
        terminal_rows = tuple(
            row
            for row in rows
            if self._is_terminal_row(row)
            and self._local_terminal_date(row.terminal_at) == today
        )
        self._set_values(
            str(len(terminal_rows)),
            str(
                sum(
                    row.judgment == Judgment.PASS.value
                    for row in terminal_rows
                )
            ),
            str(
                sum(
                    row.judgment == Judgment.FAIL.value
                    for row in terminal_rows
                )
            ),
            str(
                sum(
                    row.judgment not in _JUDGED_STATES
                    for row in terminal_rows
                )
            ),
        )

    @staticmethod
    def _is_terminal_row(row: object) -> bool:
        return (
            getattr(row, "state", None) in _TERMINAL_STATES
            and getattr(row, "terminal_at", None) is not None
        )

    @staticmethod
    def _local_terminal_date(terminal_at: str) -> date:
        terminal_utc = datetime.strptime(
            terminal_at,
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=timezone.utc)
        return terminal_utc.astimezone(_LOCAL_TIMEZONE).date()

    def _set_values(
        self,
        total: str,
        ok: str,
        ng: str,
        abnormal: str,
    ) -> None:
        self.lb_total.setText(total)
        self.lb_ok.setText(ok)
        self.lb_ng.setText(ng)
        self.lb_abnormal.setText(abnormal)
