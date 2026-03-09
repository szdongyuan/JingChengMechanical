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


class ResultModule(QWidget):
    module_id = "result"

    def __init__(self, services):
        super().__init__()
        self.services = services

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # 顶部：总结果 + 徽章
        top = QWidget()
        top.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        top_lay.addWidget(QLabel("总结果："))

        self.lb_overall = QLabel("-")
        self.lb_overall.setObjectName("ResultBadge")
        self.lb_overall.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_overall.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.lb_overall.setMinimumHeight(36)
        self.lb_overall.setMinimumWidth(180)
        self.lb_overall.setProperty("resultLevel", "unknown")
        top_lay.addStretch(1)
        top_lay.addWidget(self.lb_overall)
        lay.addWidget(top)

        # 表格：项目 / 结果
        self.table = QTableWidget(2, 2)
        self.table.setHorizontalHeaderLabels(["项目", "结果"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 120)
        self.table.horizontalHeader().setStretchLastSection(True)

        self._set_row(0, "AI评分", "-")
        self._set_row(1, "声压级", "-")
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.table.setFixedHeight(
            self.table.horizontalHeader().height() + self.table.rowHeight(0) * 2 + 18
        )
        lay.addWidget(self.table)

        # 空状态：填满剩余空间（初始化默认显示；有结果后隐藏）
        self.empty_state = QWidget()
        self.empty_state.setObjectName("EmptyState")
        empty_lay = QVBoxLayout(self.empty_state)
        empty_lay.setContentsMargins(0, 8, 0, 0)
        empty_lay.setSpacing(8)
        empty_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lb_empty_icon = QLabel("?")
        self.lb_empty_icon.setObjectName("EmptyIcon")
        self.lb_empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.lb_empty_icon)

        self.lb_empty_text = QLabel("No data")
        self.lb_empty_text.setObjectName("EmptyText")
        self.lb_empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.lb_empty_text)

        # 让空状态在剩余空间里居中；有数据时隐藏，但上下伸缩仍会吃掉多余高度，
        # 避免“总结果徽章”被拉伸成一条高条。
        lay.addStretch(1)
        lay.addWidget(self.empty_state, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)

        self.services.bus.result_changed.connect(self._on_result)

        self._set_empty_visible(True)

    def _set_row(self, row: int, name: str, value: str):
        it_name = QTableWidgetItem(name)
        it_name.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it_value = QTableWidgetItem(value)
        it_value.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it_value.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self.table.setItem(row, 0, it_name)
        self.table.setItem(row, 1, it_value)

    def _set_empty_visible(self, visible: bool):
        self.empty_state.setVisible(bool(visible))

    def _on_result(self, r: dict):
        overall = r.get("overall", None)
        if overall == "不合格":
            self.lb_overall.setText("FAIL")
            self.lb_overall.setProperty("resultLevel", "fail")
        elif overall == "合格":
            self.lb_overall.setText("PASS")
            self.lb_overall.setProperty("resultLevel", "pass")
        else:
            self.lb_overall.setText("-")
            self.lb_overall.setProperty("resultLevel", "unknown")
        # 触发 QSS 重新计算
        self.lb_overall.style().unpolish(self.lb_overall)
        self.lb_overall.style().polish(self.lb_overall)

        ai = r.get("ai_score", "-")
        try:
            ai_text = f"{float(ai):.1f}"
        except Exception:
            ai_text = str(ai) if ai not in (None, "") else "-"

        spl = r.get("spl_db", "-")
        try:
            spl_text = f"{float(spl):.1f} dB"
        except Exception:
            spl_text = f"{spl} dB" if spl not in (None, "", "-") else "-"

        self._set_row(0, "AI评分", ai_text)
        self._set_row(1, "声压级", spl_text)

        has_data = overall in ("合格", "不合格") or ai_text != "-" or spl_text != "-"
        self._set_empty_visible(not has_data)

