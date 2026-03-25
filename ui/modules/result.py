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
    _EMPTY_TEXT = "No data"
    _NO_RESULT_STATUS_TEXT = "无结果"
    _AI_STATUS_DISPLAY = {
        "success": "分析完成",
        "not_enabled": "AI 未启用",
        "not_configured": "AI 未配置",
        "preflight_error": "前置校验失败",
        "timeout": "超时",
        "process_error": "执行错误",
        "protocol_error": "协议错误",
        "invalid_output": "输出无效",
        "cancelled": "已取消",
        "no_result": _NO_RESULT_STATUS_TEXT,
        "window_invalidated": "窗口已变更，结果已失效，请确认后重新分析",
    }

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
        self.table = QTableWidget(8, 2)
        self.table.setHorizontalHeaderLabels(["项目", "结果"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 120)
        self.table.horizontalHeader().setStretchLastSection(True)

        # Row map: 0=AI标签, 1=AI评分, 2=AI简述, 3=工具链, 4=AI状态, 5=错误信息, 6=详情/产物, 7=声压级
        self._set_row(0, "AI 标签", "-")
        self._set_row(1, "AI 评分", "-")
        self._set_row(2, "AI 简述", "-")
        self._set_row(3, "工具链", "-")
        self._set_row(4, "AI 状态", "-")
        self._set_row(5, "错误信息", "-")
        self._set_row(6, "详情/产物", "-")
        self._set_row(7, "声压级", "-")
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.table.setFixedHeight(
            self.table.horizontalHeader().height() + self.table.rowHeight(0) * 8 + 18
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

        self.lb_empty_text = QLabel(self._EMPTY_TEXT)
        self.lb_empty_text.setObjectName("EmptyText")
        self.lb_empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.lb_empty_text)

        # 让空状态在剩余空间里居中；有数据时隐藏，但上下伸缩仍会吃掉多余高度，
        # 避免“总结果徽章”被拉伸成一条高条。
        lay.addStretch(1)
        lay.addWidget(self.empty_state, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)

        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)

        self._clear_result()

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

    def _reset_badge(self):
        self.lb_overall.setText("-")
        self.lb_overall.setProperty("resultLevel", "unknown")
        self.lb_overall.style().unpolish(self.lb_overall)
        self.lb_overall.style().polish(self.lb_overall)

    def _clear_result(self):
        self._reset_badge()
        self.lb_empty_icon.setText("?")
        self.lb_empty_text.setText(self._EMPTY_TEXT)
        self._set_row(0, "AI 标签", "-")
        self._set_row(1, "AI 评分", "-")
        self._set_row(2, "AI 简述", "-")
        self._set_row(3, "工具链", "-")
        self._set_row(4, "AI 状态", "-")
        self._set_row(5, "错误信息", "-")
        self._set_row(6, "详情/产物", "-")
        self._set_row(7, "声压级", "-")
        self._set_empty_visible(True)

    def _show_no_result(self, error_message: str | None):
        self._reset_badge()
        message = str(error_message) if error_message else "-"
        self.lb_empty_icon.setText("!")
        self.lb_empty_text.setText(error_message or self._NO_RESULT_STATUS_TEXT)
        self._set_row(0, "AI 标签", "-")
        self._set_row(1, "AI 评分", "-")
        self._set_row(2, "AI 简述", "-")
        self._set_row(3, "工具链", "-")
        self._set_row(4, "AI 状态", self._NO_RESULT_STATUS_TEXT)
        self._set_row(5, "错误信息", message)
        self._set_row(6, "详情/产物", "-")
        self._set_row(7, "声压级", "-")
        self._set_empty_visible(True)

    def _on_factory_display_reset(self, _reason: str):
        self._clear_result()

    def _on_factory_review_changed(self, review):
        judgment_payload = getattr(review, "judgment_payload", None)
        no_result = bool(getattr(review, "no_result", False))
        error_message = getattr(review, "error_message", None)

        if isinstance(judgment_payload, dict):
            payload = dict(judgment_payload)
            if error_message and not payload.get("error_summary"):
                payload["error_summary"] = error_message
            self._render_result(payload)
            return

        if no_result or error_message:
            self._show_no_result(error_message)
            return

        self._clear_result()

    def _render_result(self, r: dict):
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
        self.lb_overall.style().unpolish(self.lb_overall)
        self.lb_overall.style().polish(self.lb_overall)

        # AI fields — consume get_ai_view_model() fields from the payload
        ai_label = r.get("ai_label", "-") or "-"
        ai_score = r.get("ai_score", "-")
        ai_summary = r.get("ai_summary", "-") or "-"
        ai_status = r.get("display_status") or r.get("ai_status") or "-"

        try:
            ai_score_text = f"{float(ai_score):.2f}"
        except (TypeError, ValueError):
            ai_score_text = str(ai_score) if ai_score not in (None, "") else "-"

        ai_status_text = self._AI_STATUS_DISPLAY.get(ai_status, ai_status)

        # Toolchain info
        tc_name = r.get("toolchain_name", "")
        tc_version = r.get("toolchain_version", "")
        if tc_name:
            tc_text = f"{tc_name} (v{tc_version})" if tc_version else tc_name
        else:
            tc_text = "-"

        self._set_row(0, "AI 标签", str(ai_label))
        self._set_row(1, "AI 评分", ai_score_text)
        self._set_row(2, "AI 简述", str(ai_summary))
        self._set_row(3, "工具链", tc_text)
        self._set_row(4, "AI 状态", ai_status_text)

        error_summary = r.get("error_summary", "-") or "-"
        self._set_row(5, "错误信息", str(error_summary))

        # Detail/artifact entry — show artifact count or "-"
        artifacts_json = r.get("artifacts_json")
        if artifacts_json and ai_status == "success":
            import json as _json
            try:
                arts = _json.loads(artifacts_json) if isinstance(artifacts_json, str) else artifacts_json
                detail_text = f"{len(arts)} 个产物" if arts else "无产物"
            except Exception:
                detail_text = "-"
        else:
            detail_text = "-"
        self._set_row(6, "详情/产物", detail_text)

        spl = r.get("spl_db", "-")
        try:
            spl_text = f"{float(spl):.1f} dB"
        except (TypeError, ValueError):
            spl_text = f"{spl} dB" if spl not in (None, "", "-") else "-"
        self._set_row(7, "声压级", spl_text)

        has_data = (
            overall in ("合格", "不合格")
            or ai_label != "-"
            or spl_text != "-"
            or ai_status not in ("-", "no_result")
        )
        self._set_empty_visible(not has_data)

