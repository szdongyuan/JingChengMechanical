from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QFontMetrics, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.service.result_history import (
    ConfigSummaryField,
    ErrorDisplayPayload,
    FrozenConfigSummary,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultPhase,
    ReviewDisplayPayload,
)
from ui.widgets.result_history_card import ResultHistoryCard


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _close_widgets(qapp):
    yield
    for widget in qapp.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


def _snapshot(
    *,
    run_id: str = "run-<unsafe>",
    sn: str = "SN-007",
    config_name: str = "轴承终检",
    config_details: tuple[ConfigSummaryField, ...] | None = None,
    trigger_source: str = "PLC",
    phase: ResultPhase = ResultPhase.TERMINAL,
    conclusion: ResultConclusion | None = ResultConclusion.OK,
    reason: str | None = "判定完成",
    fault_type: str | None = None,
) -> ResultHistoryCardSnapshot:
    terminal = phase in (ResultPhase.TERMINAL, ResultPhase.REANALYZING)
    if config_details is None:
        config_details = (
            ConfigSummaryField("转速", "1500 rpm"),
            ConfigSummaryField("通道", "振动 1"),
        )
    return ResultHistoryCardSnapshot(
        run_id=run_id,
        config=FrozenConfigSummary(
            config_id=12,
            name=config_name,
            details=config_details,
            fault_types=((fault_type,) if fault_type is not None else ()),
        ),
        sn=sn,
        trigger_source=trigger_source,
        started_at=datetime(2026, 7, 26, 8, 30, 5),
        terminated_at=(
            datetime(2026, 7, 26, 8, 31, 10) if terminal else None
        ),
        phase=phase,
        conclusion=conclusion,
        reason=reason,
        is_active_run=not terminal,
        manual_judgment_allowed=phase is ResultPhase.TERMINAL,
        manual_reanalysis_allowed=phase is ResultPhase.TERMINAL,
        revision=3,
        operation_id="operation-1" if phase is ResultPhase.REANALYZING else None,
        fault_type=fault_type,
    )


def _visible_text(widget: QWidget) -> str:
    return "\n".join(
        label.text()
        for label in widget.findChildren(QLabel)
        if not label.isHidden()
    )


_REMOVED_OPERATOR_FIELDS = (
    "配置快照",
    "转速",
    "通道",
    "开始时间：",
    "结束时间：",
    "阶段：",
    "结论：",
    "原因：",
    "故障类型：",
    "已选中",
    "可选择",
    "选择已锁定",
)


_SUMMARY_LABELS = [
    "声压峰值",
    "声压平均",
    "响度峰值",
    "响度平均",
    "频谱得分",
    "AI 分析",
]


def _summary_table(card):
    table = card.findChild(QTableWidget, "result_history_card_summary_table")
    assert table is not None
    assert table.rowCount() == 6
    assert [table.item(row, 0).text() for row in range(6)] == _SUMMARY_LABELS
    return table


def _status_font_size(label: QLabel) -> int:
    match = re.search(r"font-size:\s*(\d+)px", label.styleSheet())
    assert match is not None
    stylesheet_size = int(match.group(1))
    assert label.font().pixelSize() == stylesheet_size
    return stylesheet_size


def _status_text_width(
    label: QLabel,
    text: str,
    pixel_size: int,
) -> int:
    effective_font = label.font()
    effective_font.setPixelSize(pixel_size)
    return QFontMetrics(effective_font).horizontalAdvance(text)


def _assert_complete_status_uses_largest_fitting_size(
    label: QLabel,
    full_status: str,
) -> int:
    available_width = label.contentsRect().width()
    assert available_width > 0
    chosen_size = _status_font_size(label)
    assert _status_text_width(label, full_status, chosen_size) <= available_width
    if chosen_size < 32:
        assert (
            _status_text_width(label, full_status, chosen_size + 1)
            > available_width
        )
    return chosen_size


def _required_wrapped_result_height(
    table: QTableWidget,
    text: str,
) -> int:
    available_width = max(1, table.columnWidth(3) - 12)
    bounds = table.fontMetrics().boundingRect(
        QRect(0, 0, available_width, 10_000),
        Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
        text,
    )
    return max(23, bounds.height() + 8)


def test_summary_table_fits_without_scrollbars_and_uses_current_only_compatibility(qapp):
    rows = [
        {"item_key": "spl", "current_display": "1.20 A"},
        {"item_key": "spl_average", "channel_label": "2.000 mA"},
        {"item_key": "loudness", "channel_label": "CH1"},
        {"item_key": "loudness_average", "channel_label": "ch2"},
        {"item_key": "stft_score", "current_display": ""},
        {"item_key": "ai", "current_display": "--"},
    ]
    card = ResultHistoryCard(
        _snapshot(),
        payload=ReviewDisplayPayload(
            review={"summary_payload": {"rows": rows}},
            dashboard_config={},
        ),
    )
    card.resize(360, card.sizeHint().height())
    card.show()
    qapp.processEvents()
    table = _summary_table(card)

    assert table.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert table.verticalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert not table.horizontalScrollBar().isVisible()
    assert not table.verticalScrollBar().isVisible()
    assert table.horizontalHeaderItem(2).text() == "电流"
    assert [table.item(row, 2).text() for row in range(6)] == [
        "1.20 A",
        "2.00 mA",
        "-",
        "-",
        "-",
        "--",
    ]
    assert sum(table.columnWidth(column) for column in range(4)) <= table.viewport().width()
    assert sum(table.rowHeight(row) for row in range(6)) <= table.viewport().height()


def test_summary_table_keeps_compact_non_wrapped_rows_and_grows_wrapped_rows(
    qapp,
):
    long_ai_result = "不通过（FAN BLADE INSTALLATION ABNORMAL）"
    rows = [
        {
            "item_key": item_key,
            "actual_display": "65.00 dB @ 1000 Hz",
            "current_display": "12.345 mA",
            "result_display": (
                long_ai_result if item_key == "ai" else "不通过"
            ),
            "status": "fail",
        }
        for item_key in (
            "spl",
            "spl_average",
            "loudness",
            "loudness_average",
            "stft_score",
            "ai",
        )
    ]
    card = ResultHistoryCard(
        _snapshot(),
        payload=ReviewDisplayPayload(
            review={"summary_payload": {"rows": rows}},
            dashboard_config={},
        ),
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    table = _summary_table(card)

    assert table.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert table.verticalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert not table.horizontalScrollBar().isVisible()
    assert not table.verticalScrollBar().isVisible()
    assert all(table.rowHeight(row) >= 23 for row in range(5))
    required_height = _required_wrapped_result_height(table, long_ai_result)
    assert required_height > 23
    assert table.rowHeight(5) >= required_height
    assert table.item(5, 3).text() == long_ai_result
    assert table.item(5, 3).toolTip() == long_ai_result
    assert [table.item(row, 2).text() for row in range(6)] == [
        "12.35 mA"
    ] * 6
    assert sum(table.rowHeight(row) for row in range(6)) <= table.viewport().height()


def test_summary_table_preserves_and_fits_long_ai_result(qapp):
    long_ai_result = "不通过（FAN BLADE INSTALLATION ABNORMAL）"
    rows = [
        {
            "item_key": "ai",
            "actual_display": "0.42",
            "current_display": "1.20 A",
            "result_display": long_ai_result,
            "status": "fail",
        }
    ]
    card = ResultHistoryCard(
        _snapshot(
            conclusion=ResultConclusion.NG,
            fault_type="风扇异常",
        ),
        payload=ReviewDisplayPayload(
            review={"summary_payload": {"rows": rows}},
            dashboard_config={},
        ),
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    table = _summary_table(card)

    assert table.item(5, 3).text() == long_ai_result
    assert table.item(5, 3).toolTip() == long_ai_result
    assert table.wordWrap() is True
    assert table.columnWidth(3) > table.columnWidth(2)
    required_height = _required_wrapped_result_height(table, long_ai_result)
    assert required_height > 23
    assert table.rowHeight(5) >= required_height
    assert (
        table.horizontalHeader().height()
        + sum(table.rowHeight(row) for row in range(6))
        <= table.height()
    )
    assert sum(table.rowHeight(row) for row in range(6)) <= table.viewport().height()
    assert not table.horizontalScrollBar().isVisible()
    assert not table.verticalScrollBar().isVisible()


def test_in_place_update_refits_long_ai_result(qapp):
    snapshot = _snapshot()
    short_payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "rows": [
                    {
                        "item_key": "ai",
                        "result_display": "PASS",
                        "status": "pass",
                    }
                ]
            }
        },
        dashboard_config={},
    )
    card = ResultHistoryCard(snapshot, payload=short_payload)
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    table = _summary_table(card)
    initial_row_height = table.rowHeight(5)
    initial_table_height = table.height()

    long_ai_result = "不通过（FAN BLADE INSTALLATION ABNORMAL）"
    long_payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "rows": [
                    {
                        "item_key": "ai",
                        "result_display": long_ai_result,
                        "status": "fail",
                    }
                ]
            }
        },
        dashboard_config={},
    )
    card.update_snapshot(snapshot, long_payload)
    qapp.processEvents()
    qapp.processEvents()

    assert _summary_table(card) is table
    assert table.item(5, 3).text() == long_ai_result
    assert table.item(5, 3).toolTip() == long_ai_result
    assert table.wordWrap() is True
    assert table.columnWidth(3) > table.columnWidth(2)
    required_height = _required_wrapped_result_height(table, long_ai_result)
    assert required_height > initial_row_height
    assert table.rowHeight(5) >= required_height
    assert table.height() > initial_table_height
    assert (
        table.horizontalHeader().height()
        + sum(table.rowHeight(row) for row in range(6))
        <= table.height()
    )
    assert sum(table.rowHeight(row) for row in range(6)) <= table.viewport().height()
    assert not table.horizontalScrollBar().isVisible()
    assert not table.verticalScrollBar().isVisible()


def test_card_action_slot_is_collapsed_until_actions_are_attached(qapp):
    card = ResultHistoryCard(_snapshot())
    slot = card.manual_judgment_slot

    assert slot.parentWidget() is card
    assert slot.objectName() == "result_history_card_action_slot"
    assert slot.height() == 0
    assert slot.isHidden()
    assert slot.focusPolicy() is Qt.FocusPolicy.NoFocus

    actions = QWidget()
    actions.setMinimumHeight(48)
    card.set_manual_judgment_widget(actions)

    assert actions.parentWidget() is slot
    assert slot.height() >= 48
    assert not slot.isHidden()

    card.set_manual_judgment_widget(None)
    assert slot.height() == 0
    assert slot.isHidden()


def test_unchanged_card_state_setters_do_not_repolish(qapp, monkeypatch):
    card = ResultHistoryCard(_snapshot())
    refreshes = []
    monkeypatch.setattr(card, "_refresh_style", lambda: refreshes.append(True))

    card.set_selected(False)
    card.set_selection_enabled(True)

    assert refreshes == []

    card.set_selected(True)
    card.set_selected(True)
    card.set_selection_enabled(False)
    card.set_selection_enabled(False)

    assert refreshes == [True, True]


def test_selected_and_focus_borders_keep_base_one_pixel_geometry(qapp):
    style_sheet = ResultHistoryCard(_snapshot()).styleSheet()

    selected_rule = re.search(
        r'QFrame#result_history_card\[selected="true"\]\s*\{(?P<body>[^}]+)\}',
        style_sheet,
    )
    focus_rule = re.search(
        r"QFrame#result_history_card:focus\s*\{(?P<body>[^}]+)\}",
        style_sheet,
    )

    assert selected_rule is not None
    assert focus_rule is not None
    assert "border-top: 1px" in selected_rule.group("body")
    assert "border-right: 1px" in selected_rule.group("body")
    assert "border-bottom: 1px" in selected_rule.group("body")
    assert "border-top: 1px" in focus_rule.group("body")
    assert "border-right: 1px" in focus_rule.group("body")
    assert "border-bottom: 1px" in focus_rule.group("body")


@pytest.mark.parametrize(
    "phase",
    [
        ResultPhase.RECORDING,
        ResultPhase.WAITING_SAVE,
        ResultPhase.WAITING_ANALYSIS,
        ResultPhase.ANALYZING,
    ],
)
def test_active_card_always_renders_fixed_summary_skeleton(qapp, phase):
    card = ResultHistoryCard(
        _snapshot(phase=phase, conclusion=None, reason=None)
    )
    card.show()
    qapp.processEvents()

    caption = card.findChild(QLabel, "result_history_card_summary_caption")
    overall = card.findChild(QLabel, "result_history_card_summary_overall")
    assert caption is not None
    assert overall is not None
    assert caption.text() == "结果摘要"
    assert overall.text() == "进行中"
    assert card.findChild(QLabel, "result_history_card_metric_spl").text() == "--"
    assert card.findChild(QLabel, "result_history_card_metric_loudness").text() == "--"
    table = _summary_table(card)
    assert [table.item(row, 1).text() for row in range(6)] == ["-"] * 6
    assert [table.item(row, 3).text() for row in range(6)] == ["-"] * 6


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        (ResultConclusion.OK, "通过"),
        (ResultConclusion.NG, "不通过"),
        (ResultConclusion.NO_RESULT, "无结果"),
        (ResultConclusion.ERROR, "失败"),
        (ResultConclusion.CAPACITY_REJECTED, "容量已满"),
        (ResultConclusion.MANUAL_STOPPED, "已停止"),
    ],
)
def test_terminal_card_without_review_keeps_fixed_summary_skeleton(
    qapp, conclusion, expected
):
    payload = ErrorDisplayPayload("analysis", "boom") if conclusion is ResultConclusion.ERROR else None
    card = ResultHistoryCard(
        _snapshot(conclusion=conclusion, reason="boom"),
        payload=payload,
    )

    caption = card.findChild(QLabel, "result_history_card_summary_caption")
    overall = card.findChild(QLabel, "result_history_card_summary_overall")
    assert caption is not None
    assert overall is not None
    assert caption.text() == "结果摘要"
    assert overall.text() == expected
    _summary_table(card)


def test_ng_overall_includes_frozen_fault_type(qapp):
    card = ResultHistoryCard(
        _snapshot(conclusion=ResultConclusion.NG, fault_type="异响")
    )

    caption = card.findChild(QLabel, "result_history_card_summary_caption")
    overall = card.findChild(QLabel, "result_history_card_summary_overall")
    assert caption is not None
    assert overall is not None
    assert caption.text() == "结果摘要"
    assert overall.text() == "不通过：异响"


@pytest.mark.parametrize(
    ("conclusion", "fault_type", "expected"),
    [
        (
            ResultConclusion.NG,
            "异响",
            "不通过：异响",
        ),
        (ResultConclusion.NO_RESULT, None, "无结果"),
    ],
)
def test_failure_card_renders_large_centered_single_line_overall_status(
    qapp,
    conclusion,
    fault_type,
    expected,
):
    card = ResultHistoryCard(
        _snapshot(
            conclusion=conclusion,
            fault_type=fault_type,
        )
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )
    caption = card.findChild(
        QLabel,
        "result_history_card_summary_caption",
    )

    assert card.width() == 360
    assert caption is not None
    assert overall is not None
    assert caption.text() == "结果摘要"
    assert not caption.isHidden()
    assert overall.text() == expected
    assert overall.textFormat() is Qt.TextFormat.PlainText
    assert overall.accessibleName() == "结果摘要总结果"
    assert expected in overall.accessibleDescription()
    assert overall.wordWrap() is False
    assert overall.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert (
        overall.sizePolicy().horizontalPolicy()
        is QSizePolicy.Policy.Expanding
    )
    assert (
        overall.sizePolicy().verticalPolicy()
        is QSizePolicy.Policy.Fixed
    )
    assert overall.minimumHeight() == 48
    assert overall.maximumHeight() == 48
    assert overall.height() == 48
    assert "font-size: 32px" in overall.styleSheet()
    assert "font-weight: 800" in overall.styleSheet()
    assert "#d93025" in overall.styleSheet().lower()


def test_single_line_four_character_fault_stays_complete_at_default_size(qapp):
    full_status = "不通过：风扇异常"
    card = ResultHistoryCard(
        _snapshot(
            conclusion=ResultConclusion.NG,
            fault_type="风扇异常",
        )
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )

    assert card.width() == 360
    assert overall is not None
    assert overall.text() == full_status
    assert overall.toolTip() == f"结果摘要：{full_status}"
    assert full_status in overall.accessibleDescription()
    assert overall.wordWrap() is False
    assert overall.height() == 48
    assert "font-size: 32px" in overall.styleSheet()
    assert _assert_complete_status_uses_largest_fitting_size(
        overall,
        full_status,
    ) == 32


def test_adaptive_status_shrinks_to_keep_long_fault_complete(qapp):
    fault_type = "风扇叶片安装异常"
    full_status = f"不通过：{fault_type}"
    configured_ng = _snapshot(
        conclusion=ResultConclusion.NG,
        fault_type=fault_type,
    )
    baseline = ResultHistoryCard(
        replace(
            configured_ng,
            conclusion=ResultConclusion.OK,
            fault_type=None,
        )
    )
    baseline.setFixedWidth(360)
    baseline.show()
    qapp.processEvents()
    qapp.processEvents()
    baseline_height = baseline.height()

    card = ResultHistoryCard(
        configured_ng
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )

    assert overall is not None
    assert overall.text() == full_status
    assert overall.wordWrap() is False
    assert overall.height() == 48
    assert card.height() == baseline_height
    chosen_size = _assert_complete_status_uses_largest_fitting_size(
        overall,
        full_status,
    )
    assert 18 <= chosen_size <= 31
    assert overall.toolTip() == f"结果摘要：{full_status}"
    assert full_status in overall.accessibleDescription()


def test_adaptive_status_elides_at_minimum_size_but_keeps_full_metadata(qapp):
    fault_type = "风扇叶片安装方向和固定状态异常需要立即停机进行人工复核"
    assert len(fault_type.encode("utf-8")) <= 128
    full_status = f"不通过：{fault_type}"
    card = ResultHistoryCard(
        _snapshot(
            conclusion=ResultConclusion.NG,
            fault_type=fault_type,
        )
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )

    assert overall is not None
    available_width = overall.contentsRect().width()
    assert available_width > 0
    assert _status_font_size(overall) == 18
    assert _status_text_width(overall, full_status, 18) > available_width
    assert overall.text() != full_status
    assert overall.text().endswith("…")
    assert full_status.startswith(overall.text()[:-1])
    assert _status_text_width(overall, overall.text(), 18) <= available_width
    assert overall.wordWrap() is False
    assert overall.height() == 48
    assert overall.toolTip() == f"结果摘要：{full_status}"
    assert full_status in overall.accessibleDescription()


def test_adaptive_status_refits_on_in_place_summary_update(qapp):
    fault_type = "风扇叶片安装异常"
    full_status = f"不通过：{fault_type}"
    updated = _snapshot(
        conclusion=ResultConclusion.NG,
        fault_type=fault_type,
    )
    initial = replace(
        updated,
        conclusion=ResultConclusion.OK,
        fault_type=None,
    )
    card = ResultHistoryCard(initial)
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )
    assert overall is not None
    initial_card_height = card.height()
    assert overall.text() == "通过"
    assert _status_font_size(overall) == 32

    card.update_snapshot(updated, None)
    qapp.processEvents()
    qapp.processEvents()

    assert card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    ) is overall
    assert overall.text() == full_status
    chosen_size = _assert_complete_status_uses_largest_fitting_size(
        overall,
        full_status,
    )
    assert 18 <= chosen_size <= 31
    assert overall.height() == 48
    assert card.height() == initial_card_height
    assert overall.toolTip() == f"结果摘要：{full_status}"
    assert full_status in overall.accessibleDescription()


def test_manual_preview_refits_ng_then_restores_ok_default_size(qapp):
    fault_type = "风扇叶片安装异常"
    configured_ng = _snapshot(
        conclusion=ResultConclusion.NG,
        fault_type=fault_type,
    )
    card = ResultHistoryCard(
        replace(
            configured_ng,
            conclusion=ResultConclusion.OK,
            fault_type=None,
        )
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )
    assert overall is not None
    initial_card_height = card.height()
    full_status = f"不通过：{fault_type}"

    card.preview_manual_judgment("NG", fault_type)
    qapp.processEvents()
    qapp.processEvents()

    assert card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    ) is overall
    assert overall.text() == full_status
    chosen_size = _assert_complete_status_uses_largest_fitting_size(
        overall,
        full_status,
    )
    assert 18 <= chosen_size <= 31
    assert overall.height() == 48
    assert card.height() == initial_card_height
    assert overall.toolTip() == f"结果摘要：{full_status}"
    assert full_status in overall.accessibleDescription()

    card.preview_manual_judgment("OK", None)
    qapp.processEvents()
    qapp.processEvents()

    assert card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    ) is overall
    assert overall.text() == "通过"
    assert overall.height() == 48
    assert card.height() == initial_card_height
    assert overall.minimumHeight() == 48
    assert overall.maximumHeight() == 48
    assert overall.textFormat() is Qt.TextFormat.PlainText
    assert overall.accessibleName() == "结果摘要总结果"
    assert "通过" in overall.accessibleDescription()
    assert overall.wordWrap() is False
    assert overall.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert "font-size: 32px" in overall.styleSheet()
    assert _assert_complete_status_uses_largest_fitting_size(
        overall,
        "通过",
    ) == 32
    assert "font-weight: 800" in overall.styleSheet()
    assert "#188038" in overall.styleSheet().lower()


def test_adaptive_status_refits_same_label_when_card_narrows_then_widens(qapp):
    full_status = "不通过：风扇异常"
    card = ResultHistoryCard(
        _snapshot(
            conclusion=ResultConclusion.NG,
            fault_type="风扇异常",
        )
    )
    card.setFixedWidth(360)
    card.show()
    qapp.processEvents()
    qapp.processEvents()
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )
    assert overall is not None
    initial_card_height = card.height()
    assert overall.text() == full_status
    assert _status_font_size(overall) == 32
    assert overall.height() == 48

    card.setFixedWidth(220)
    qapp.processEvents()
    qapp.processEvents()

    assert card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    ) is overall
    available_width = overall.contentsRect().width()
    assert available_width > 0
    chosen_size = _status_font_size(overall)
    assert chosen_size < 32
    assert (
        _status_text_width(overall, overall.text(), chosen_size)
        <= available_width
    )
    if overall.text() == full_status:
        assert (
            _status_text_width(overall, full_status, chosen_size + 1)
            > available_width
        )
    else:
        assert chosen_size == 18
        assert _status_text_width(overall, full_status, 18) > available_width
        assert overall.text().endswith("…")
    assert overall.height() == 48
    assert card.height() == initial_card_height
    assert overall.toolTip() == f"结果摘要：{full_status}"
    assert full_status in overall.accessibleDescription()

    card.setFixedWidth(360)
    qapp.processEvents()
    qapp.processEvents()

    assert card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    ) is overall
    assert overall.text() == full_status
    assert _assert_complete_status_uses_largest_fitting_size(
        overall,
        full_status,
    ) == 32
    assert overall.height() == 48
    assert card.height() == initial_card_height


@pytest.mark.parametrize(
    ("overall_label", "expected"),
    [
        ("不通过", "不通过：异响"),
        ("不通过（异响）", "不通过：异响"),
    ],
)
def test_ng_review_overall_canonicalizes_frozen_fault_type_exactly_once(
    qapp,
    overall_label,
    expected,
):
    payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_label": overall_label,
                "rows": [],
            }
        },
        dashboard_config={},
    )
    card = ResultHistoryCard(
        _snapshot(conclusion=ResultConclusion.NG, fault_type="异响"),
        payload=payload,
    )

    overall = card.findChild(
        QLabel, "result_history_card_summary_overall"
    )
    caption = card.findChild(
        QLabel, "result_history_card_summary_caption"
    )
    assert caption is not None
    assert overall is not None
    assert caption.text() == "结果摘要"
    assert overall.text() == expected


def test_operator_card_omits_duplicate_metadata_and_selection_text(qapp):
    card = ResultHistoryCard(
        _snapshot(
            conclusion=ResultConclusion.NG,
            fault_type="异响",
            reason="人工复核",
        )
    )
    card.set_selected(True)
    card.show()
    qapp.processEvents()

    text = _visible_text(card)
    assert "配置：轴承终检" in text
    assert "SN：SN-007" in text
    assert "触发：PLC" in text
    for duplicate in _REMOVED_OPERATOR_FIELDS:
        assert duplicate not in text


def test_card_compacts_long_sn_and_shows_sn_with_trigger_on_one_row(qapp):
    full_sn = "ABCDEF-1234567890-UVWXYZ"
    card = ResultHistoryCard(
        _snapshot(sn=full_sn, trigger_source="plc")
    )

    sn_label = card.findChild(QLabel, "result_history_card_sn")
    trigger_label = card.findChild(
        QLabel,
        "result_history_card_trigger_source",
    )
    assert sn_label.text() == "SN：ABCDEF...UVWXYZ"
    assert sn_label.toolTip() == full_sn
    assert trigger_label.text() == "触发：PLC"
    assert sn_label.parentWidget() is trigger_label.parentWidget()


def test_card_renders_compact_legacy_result_summary_payload(qapp):
    payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_status": "fail",
                "overall_label": "不通过",
                "rows": [
                    {
                        "item_key": "spl",
                        "label": "声压级（峰值）",
                        "actual_display": "81.25 dB",
                        "channel_label": "CH1",
                        "status": "fail",
                        "result_display": "不通过",
                    },
                    {
                        "item_key": "stft_score",
                        "label": "频谱最低得分",
                        "actual_display": "72.00",
                        "current_display": "1.20 A",
                        "status": "pass",
                        "result_display": "通过",
                    },
                ],
            }
        },
        dashboard_config={"config_id": 12},
    )

    card = ResultHistoryCard(_snapshot(), payload=payload)
    table = card.findChild(
        QTableWidget,
        "result_history_card_summary_table",
    )

    caption = card.findChild(QLabel, "result_history_card_summary_caption")
    overall = card.findChild(QLabel, "result_history_card_summary_overall")
    assert caption is not None
    assert overall is not None
    assert caption.text() == "结果摘要"
    assert overall.text() == "不通过"
    assert table is not None
    assert table.rowCount() == 6
    assert table.item(0, 0).text() == "声压峰值"
    assert table.item(0, 1).text() == "81.25 dB"
    assert table.item(4, 2).text() == "1.20 A"
    for duplicate in _REMOVED_OPERATOR_FIELDS:
        assert duplicate not in _visible_text(card)


@pytest.mark.parametrize(
    ("overall_status", "overall_label", "conclusion", "terminal_color"),
    [
        ("pass", "通过", ResultConclusion.OK, "#188038"),
        ("fail", "不通过", ResultConclusion.NG, "#d93025"),
    ],
)
def test_reanalyzing_uses_neutral_overall_color_with_retained_payload(
    qapp,
    overall_status,
    overall_label,
    conclusion,
    terminal_color,
):
    payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_status": overall_status,
                "overall_label": overall_label,
                "rows": [],
            }
        },
        dashboard_config={},
    )
    terminal = _snapshot(conclusion=conclusion)
    card = ResultHistoryCard(terminal, payload=payload)
    overall = card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )
    assert overall is not None
    assert terminal_color in overall.styleSheet().lower()

    reanalyzing = _snapshot(
        phase=ResultPhase.REANALYZING,
        conclusion=conclusion,
    )
    card.update_snapshot(reanalyzing, payload)

    assert card.findChild(
        QLabel,
        "result_history_card_summary_overall",
    ) is overall
    assert overall.text() == "重新分析中"
    assert "#424242" in overall.styleSheet().lower()


@pytest.mark.parametrize(
    ("phase", "conclusion"),
    [
        (ResultPhase.TERMINAL, ResultConclusion.OK),
        (ResultPhase.TERMINAL, ResultConclusion.NG),
        (ResultPhase.TERMINAL, ResultConclusion.ERROR),
        (ResultPhase.REANALYZING, ResultConclusion.NG),
    ],
)
def test_card_omits_phase_conclusion_and_reason_for_every_result_state(
    qapp,
    phase,
    conclusion,
):
    card = ResultHistoryCard(
        _snapshot(phase=phase, conclusion=conclusion, reason="内部原因")
    )

    text = _visible_text(card)
    assert "阶段：" not in text
    assert "结论：" not in text
    assert "原因：" not in text
    assert "内部原因" not in text


@pytest.mark.parametrize(
    ("phase", "conclusion", "reason", "expected_state"),
    [
        (ResultPhase.ANALYZING, None, None, "active"),
        (ResultPhase.TERMINAL, ResultConclusion.OK, None, "ok"),
        (ResultPhase.TERMINAL, ResultConclusion.NG, None, "ng"),
        (ResultPhase.TERMINAL, ResultConclusion.ERROR, None, "error"),
        (
            ResultPhase.REANALYZING,
            ResultConclusion.NG,
            None,
            "reanalyzing",
        ),
        (
            ResultPhase.TERMINAL,
            ResultConclusion.NO_RESULT,
            None,
            "no_result",
        ),
        (
            ResultPhase.TERMINAL,
            ResultConclusion.CAPACITY_REJECTED,
            None,
            "capacity_rejected",
        ),
        (
            ResultPhase.TERMINAL,
            ResultConclusion.MANUAL_STOPPED,
            None,
            "manual_stopped",
        ),
    ],
)
def test_card_exposes_trusted_visual_state_property(
    qapp,
    phase,
    conclusion,
    reason,
    expected_state,
):
    unsafe_run_id = "run/<script>not-a-style-token</script>"
    card = ResultHistoryCard(
        _snapshot(
            run_id=unsafe_run_id,
            phase=phase,
            conclusion=conclusion,
            reason=reason,
        )
    )

    assert card.property("resultState") == expected_state
    assert unsafe_run_id not in card.property("resultState")


@pytest.mark.parametrize(
    ("state", "color"),
    [
        ("active", "#0d47a1"),
        ("ok", "#1b5e20"),
        ("ng", "#b71c1c"),
        ("error", "#880e4f"),
    ],
)
def test_card_stylesheet_has_recognizable_state_color_rules(
    qapp,
    state,
    color,
):
    card = ResultHistoryCard(_snapshot())
    selector = (
        f'QFrame#result_history_card[resultState="{state}"]'
    )
    match = re.search(
        rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]+)\}}",
        card.styleSheet(),
    )

    assert match is not None
    assert "border-left" in match.group("body")
    assert color in match.group("body").lower()
    assert '[selected="true"]' in card.styleSheet()
    assert '[selectionEnabled="false"]' in card.styleSheet()


def test_card_emits_run_id_only_when_selectable(qapp):
    card = ResultHistoryCard(_snapshot(run_id="run-selectable"))
    emitted: list[str] = []
    card.activated.connect(emitted.append)
    card.resize(card.sizeHint())
    card.show()
    qapp.processEvents()

    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    assert emitted == ["run-selectable"]

    card.set_selection_enabled(False)
    assert card.isEnabled()
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    assert emitted == ["run-selectable"]

    card.set_selection_enabled(True)
    card.set_selected(True)
    QTest.mouseClick(card, Qt.MouseButton.RightButton)
    assert emitted == ["run-selectable"]
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    assert emitted == ["run-selectable", "run-selectable"]


def test_card_keyboard_activation_uses_same_guard_as_mouse(qapp):
    card = ResultHistoryCard(_snapshot(run_id="keyboard-run"))
    emitted: list[str] = []
    card.activated.connect(emitted.append)
    card.show()
    card.setFocus()
    qapp.processEvents()

    assert card.focusPolicy() is Qt.FocusPolicy.StrongFocus
    assert card.hasFocus()
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
        QTest.keyClick(card, key)
    assert emitted == ["keyboard-run", "keyboard-run", "keyboard-run"]

    card.set_selection_enabled(False)
    assert card.focusPolicy() is Qt.FocusPolicy.StrongFocus
    assert card.hasFocus()
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
        QTest.keyClick(card, key)
    assert emitted == ["keyboard-run", "keyboard-run", "keyboard-run"]


def test_card_participates_in_normal_tab_focus_traversal(qapp):
    host = QWidget()
    layout = QVBoxLayout(host)
    first = ResultHistoryCard(_snapshot(run_id="first"))
    second = ResultHistoryCard(_snapshot(run_id="second"))
    layout.addWidget(first)
    layout.addWidget(second)
    host.show()
    first.setFocus()
    qapp.processEvents()

    QTest.keyClick(first, Qt.Key.Key_Tab)

    assert second.hasFocus()


def test_card_treats_config_name_sn_and_trigger_source_as_plain_text(qapp):
    config_name = "<b>配置 &amp; 名称</b>"
    detail_label = "<img src=x onerror=alert(1)>"
    detail_value = "值 &lt;仍为文本&gt;"
    sn = "<b>SN</b>&copy;"
    source = "<img src='missing'>PLC"
    reason = "<a href='x'>失败 &amp; 原因</a>"
    card = ResultHistoryCard(
        _snapshot(
            config_name=config_name,
            config_details=(
                ConfigSummaryField(detail_label, detail_value),
            ),
            sn=sn,
            trigger_source=source,
            conclusion=ResultConclusion.ERROR,
            reason=reason,
        )
    )

    labels = card.findChildren(QLabel)
    assert labels
    assert all(
        label.textFormat() is Qt.TextFormat.PlainText for label in labels
    )
    literal_text = "\n".join(label.text() for label in labels)
    assert config_name in literal_text
    assert "<b>SN" in literal_text
    assert "<img src='missing'>PLC" in literal_text
    for omitted in (
        detail_label,
        detail_value,
        reason,
    ):
        assert omitted not in literal_text


_STATE_BACKGROUNDS = {
    "active": "#e3f2fd",
    "ok": "#e8f5e9",
    "ng": "#ffebee",
    "error": "#fce4ec",
    "reanalyzing": "#f3e5f5",
    "no_result": "#f5f5f5",
    "capacity_rejected": "#fff3e0",
    "manual_stopped": "#efebe9",
}

_STATE_VALUES = {
    "active": (ResultPhase.ANALYZING, None),
    "ok": (ResultPhase.TERMINAL, ResultConclusion.OK),
    "ng": (ResultPhase.TERMINAL, ResultConclusion.NG),
    "error": (ResultPhase.TERMINAL, ResultConclusion.ERROR),
    "reanalyzing": (ResultPhase.REANALYZING, ResultConclusion.NG),
    "no_result": (ResultPhase.TERMINAL, ResultConclusion.NO_RESULT),
    "capacity_rejected": (
        ResultPhase.TERMINAL,
        ResultConclusion.CAPACITY_REJECTED,
    ),
    "manual_stopped": (
        ResultPhase.TERMINAL,
        ResultConclusion.MANUAL_STOPPED,
    ),
}


@pytest.mark.parametrize("state", _STATE_BACKGROUNDS)
@pytest.mark.parametrize("mode", ("normal", "selected", "locked_selected"))
def test_card_retains_state_background_without_duplicate_state_text(
    qapp,
    state,
    mode,
):
    phase, conclusion = _STATE_VALUES[state]
    normal_background = _STATE_BACKGROUNDS[state]
    card = ResultHistoryCard(
        _snapshot(
            phase=phase,
            conclusion=conclusion,
            reason=None,
        )
    )
    if mode in ("selected", "locked_selected"):
        card.set_selected(True)
    if mode == "locked_selected":
        card.set_selection_enabled(False)
    card.show()
    qapp.processEvents()

    rendered_background = card.palette().color(
        QPalette.ColorRole.Window
    ).name()
    expected_background = (
        "#eceff1" if mode == "locked_selected" else normal_background
    )

    assert card.property("resultState") == state
    assert rendered_background == expected_background
    assert card.findChild(QLabel, "result_history_card_conclusion") is None
    assert "阶段：" not in _visible_text(card)
    assert "结论：" not in _visible_text(card)


def test_card_exposes_stable_object_and_accessible_names(qapp):
    unsafe_run_id = "run/<script>alert(1)</script>"
    card = ResultHistoryCard(_snapshot(run_id=unsafe_run_id))

    assert card.objectName() == "result_history_card"
    assert card.accessibleName() == "结果历史卡片"
    assert card.accessibleDescription() == "结果状态：通过"
    assert card.property("run_id") == unsafe_run_id
    assert card.property("resultState") == "ok"
    assert card.toolTip()

    objects = [card, *card.findChildren(QWidget)]
    assert all(unsafe_run_id not in widget.objectName() for widget in objects)
    assert all(
        unsafe_run_id not in widget.accessibleName() for widget in objects
    )
