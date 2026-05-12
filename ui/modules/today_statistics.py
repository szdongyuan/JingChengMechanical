from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QGridLayout, QLabel, QSizePolicy, QWidget


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

        # 两行显示：第一行总数，第二行 OK / NG 数。
        layout.addWidget(QLabel("今日总测试数："), 0, 0)
        layout.addWidget(self.lb_total, 0, 1)
        layout.addWidget(QLabel("OK数："), 1, 0)
        layout.addWidget(self.lb_ok, 1, 1)
        layout.addWidget(QLabel("NG数："), 1, 2)
        layout.addWidget(self.lb_ng, 1, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # 自动分析结果变化 -> 刷新
        self.services.bus.result_changed.connect(lambda _payload: self.refresh())
        # 测试完成 -> 刷新
        self.services.bus.test_finished.connect(
            lambda _config_id, _payload: self.refresh()
        )
        # 手工 OK/NG 后 -> 刷新
        self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        self.refresh()

        # 防止长时间打开页面跨天后数据不刷新。
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def _create_value_label(self, color: str = "#2563eb") -> QLabel:
        label = QLabel("0")
        label.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {color};")
        label.setMinimumWidth(44)
        return label

    def _on_factory_display_reset(self, reason: str) -> None:
        # 手工判定 OK / NG 会改数据库里的 labels，需要刷新统计。
        if reason in {"manual_judgment_ok", "manual_judgment_ng"}:
            self.refresh()

    def refresh(self) -> None:
        # 查询 audio_data_table 中今天的总数、OK 数、NG 数。
        stats = self.services.audio_data_service.today_statistics()
        self._set_values(
            str(stats["total"]),
            str(stats["ok"]),
            str(stats["ng"]),
        )

    def _set_values(self, total: str, ok: str, ng: str) -> None:
        self.lb_total.setText(total)
        self.lb_ok.setText(ok)
        self.lb_ng.setText(ng)
