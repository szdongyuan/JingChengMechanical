from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QRect,
    QThread,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.service.result_history import (
    ConfigSummaryField,
    FrozenConfigSummary,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultHistorySnapshot,
    ResultHistoryTransition,
    ResultPhase,
    ReviewDisplayPayload,
)
from ui.modules.result_history import ResultHistoryModule
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
        if sip.isdeleted(widget):
            continue
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _card(
    run_id: str,
    *,
    active: bool = False,
    index: int = 0,
) -> ResultHistoryCardSnapshot:
    started = datetime(2026, 7, 26, 9, 0) + timedelta(minutes=index)
    return ResultHistoryCardSnapshot(
        run_id=run_id,
        config=FrozenConfigSummary(
            config_id=index,
            name=f"配置 {index}",
            details=(
                ConfigSummaryField("转速", f"{1000 + index} rpm"),
                ConfigSummaryField("测量模式", "终检"),
            ),
        ),
        sn=f"SN-{index}",
        trigger_source="PLC",
        started_at=started,
        terminated_at=None if active else started + timedelta(seconds=30),
        phase=ResultPhase.ANALYZING if active else ResultPhase.TERMINAL,
        conclusion=None if active else ResultConclusion.OK,
        reason=None if active else "检测完成",
        is_active_run=active,
        manual_judgment_allowed=not active,
        manual_reanalysis_allowed=not active,
        revision=index,
    )


def _snapshot(
    *,
    active_cards: tuple[ResultHistoryCardSnapshot, ...] = (),
    terminal_cards: tuple[ResultHistoryCardSnapshot, ...] = (),
    follow_latest: bool = True,
    selected_run_id: str | None = None,
    revision: int = 1,
) -> ResultHistorySnapshot:
    if follow_latest:
        selected_run_id = (
            active_cards[0].run_id
            if active_cards
            else terminal_cards[0].run_id if terminal_cards else None
        )
    return ResultHistorySnapshot(
        revision=revision,
        follow_latest=follow_latest,
        selected_run_id=selected_run_id,
        active_cards=active_cards,
        terminal_cards=terminal_cards,
    )


def _find(module: QWidget, widget_type, object_name: str):
    widget = module.findChild(widget_type, object_name)
    assert widget is not None
    return widget


def _card_for(module: QWidget, run_id: str) -> ResultHistoryCard:
    for card in module.findChildren(ResultHistoryCard):
        if card.property("run_id") == run_id:
            return card
    raise AssertionError(f"card not found: {run_id}")


def _metric(module: QWidget, run_id: str, name: str) -> str:
    card = _card_for(module, run_id)
    label = card.findChild(QLabel, f"result_history_card_metric_{name}")
    assert label is not None
    return label.text()


def _configs(config_id: int):
    return {
        "selected_channels": [2, 0, 1],
        "vibration_view_channel_index": 2,
        "channels": [
            {"channel_index": 0, "loudness_unit": "sone"},
            {"channel_index": 1, "loudness_unit": "phon"},
        ],
    }


def test_interleaved_live_metrics_are_isolated_by_run_and_primary_channel(qapp):
    module = ResultHistoryModule(config_provider=_configs)
    run_a = _card("run-a", active=True, index=1)
    run_b = _card("run-b", active=True, index=2)
    module.render_snapshot(_snapshot(active_cards=(run_a, run_b)))

    module.on_live_spl(1, "run-a", 0, SimpleNamespace(spl_db=61.25))
    module.on_live_spl(2, "run-b", 0, SimpleNamespace(spl_db=72.5))
    module.on_live_loudness(
        1, "run-a", 0, SimpleNamespace(loudness_sone=1.25)
    )

    assert _metric(module, "run-a", "spl") == "61.2 dB"
    assert _metric(module, "run-a", "loudness") == "1.25 sone"
    assert _metric(module, "run-b", "spl") == "72.5 dB"
    assert _metric(module, "run-b", "loudness") == "--"


def test_live_metric_rejects_wrong_channel_unknown_nonfinite_and_late_frames(qapp):
    module = ResultHistoryModule(config_provider=_configs)
    active = _card("run-a", active=True, index=1)
    module.render_snapshot(_snapshot(active_cards=(active,)))
    module.on_live_spl(1, "run-a", 0, SimpleNamespace(spl_db=60.0))
    module.on_live_spl(1, "run-a", 1, SimpleNamespace(spl_db=99.0))
    module.on_live_spl(1, "missing", 0, SimpleNamespace(spl_db=88.0))
    module.on_live_spl(1, "run-a", 0, SimpleNamespace(spl_db=float("nan")))

    assert _metric(module, "run-a", "spl") == "60.0 dB"

    terminal = replace(
        active,
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        is_active_run=False,
        terminated_at=datetime(2026, 7, 26, 9, 1),
    )
    module.render_snapshot(_snapshot(terminal_cards=(terminal,), revision=2))
    module.on_live_spl(1, "run-a", 0, SimpleNamespace(spl_db=70.0))
    assert _metric(module, "run-a", "spl") == "60.0 dB"


def test_live_fallback_survives_terminal_revision_and_state_is_cleaned(qapp):
    payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_label": "通过",
                "rows": [
                    {"item_key": "spl_average", "actual_display": "--"},
                    {"item_key": "loudness_average", "actual_display": "-"},
                ],
            }
        },
        dashboard_config={},
    )
    module = ResultHistoryModule(
        payload_provider=lambda _run_id: payload,
        config_provider=_configs,
    )
    active = _card("run-a", active=True, index=1)
    module.render_snapshot(_snapshot(active_cards=(active,)))
    module.on_live_spl(1, "run-a", 0, SimpleNamespace(spl_db=61.25))
    module.on_live_loudness(
        1, "run-a", 0, SimpleNamespace(loudness_sone=1.25)
    )
    terminal = replace(
        active,
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        is_active_run=False,
        terminated_at=datetime(2026, 7, 26, 9, 1),
    )
    module.render_snapshot(_snapshot(terminal_cards=(terminal,), revision=2))
    assert _metric(module, "run-a", "spl") == "61.2 dB"
    assert _metric(module, "run-a", "loudness") == "1.25 sone"

    module.render_snapshot(
        _snapshot(
            terminal_cards=(replace(terminal, revision=3),),
            revision=3,
        )
    )
    assert _metric(module, "run-a", "spl") == "61.2 dB"
    assert "run-a" in module._metric_view_state_by_run

    reanalyzing = replace(
        terminal,
        phase=ResultPhase.REANALYZING,
        operation_id="op-1",
        revision=4,
    )
    module.render_snapshot(
        _snapshot(terminal_cards=(reanalyzing,), revision=4)
    )
    assert _metric(module, "run-a", "spl") == "61.2 dB"
    module.render_snapshot(
        _snapshot(
            terminal_cards=(replace(terminal, revision=5),),
            revision=5,
        )
    )
    assert _metric(module, "run-a", "spl") == "61.2 dB"

    module.render_snapshot(_snapshot(revision=6))
    assert module._metric_view_state_by_run == {}


def test_module_close_clears_live_view_state(qapp):
    module = ResultHistoryModule(config_provider=_configs)
    module.render_snapshot(
        _snapshot(active_cards=(_card("run-a", active=True, index=1),))
    )
    assert "run-a" in module._metric_view_state_by_run

    module.close()

    assert module._metric_view_state_by_run == {}


def test_terminal_average_display_wins_over_live_fallback(qapp):
    payload = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_label": "通过",
                "rows": [
                    {"item_key": "spl_average", "actual_display": "55.55 dBA"},
                    {"item_key": "loudness_average", "actual_display": "3.21 phon"},
                ],
            }
        },
        dashboard_config={},
    )
    module = ResultHistoryModule(
        payload_provider=lambda _run_id: payload,
        config_provider=_configs,
    )
    active = _card("run-a", active=True, index=1)
    module.render_snapshot(_snapshot(active_cards=(active,)))
    module.on_live_spl(1, "run-a", 0, SimpleNamespace(spl_db=61.25))
    terminal = replace(
        active,
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        is_active_run=False,
        terminated_at=datetime(2026, 7, 26, 9, 1),
    )
    module.render_snapshot(_snapshot(terminal_cards=(terminal,), revision=2))

    assert _metric(module, "run-a", "spl") == "55.55 dBA"
    assert _metric(module, "run-a", "loudness") == "3.21 phon"


class _WorkerLiveEmitter(QObject):
    spl = pyqtSignal(int, str, int, object)

    @pyqtSlot()
    def emit_frame(self):
        self.spl.emit(1, "run-a", 0, SimpleNamespace(spl_db=64.0))


def test_qobject_live_delivery_updates_card_on_module_qt_thread(qapp):
    module = ResultHistoryModule(config_provider=_configs)
    module.render_snapshot(
        _snapshot(active_cards=(_card("run-a", active=True, index=1),))
    )
    card = _card_for(module, "run-a")
    update_threads = []
    original = card.update_metric_view

    def record_update(**kwargs):
        update_threads.append(QThread.currentThread())
        original(**kwargs)

    card.update_metric_view = record_update
    worker_thread = QThread()
    emitter = _WorkerLiveEmitter()
    emitter.moveToThread(worker_thread)
    emitter.spl.connect(module.on_live_spl)
    worker_thread.started.connect(emitter.emit_frame)
    worker_thread.start()
    try:
        for _ in range(50):
            qapp.processEvents()
            if update_threads:
                break
            QTest.qWait(2)
    finally:
        worker_thread.quit()
        worker_thread.wait()

    assert update_threads == [module.thread()]
    assert _metric(module, "run-a", "spl") == "64.0 dB"


def test_removed_headings_are_not_visible_but_card_overall_remains(qapp):
    module = ResultHistoryModule(config_provider=_configs)
    module.render_snapshot(
        _snapshot(active_cards=(_card("run-a", active=True, index=1),))
    )
    module.show()
    qapp.processEvents()
    visible_labels = [
        label.text()
        for label in module.findChildren(QLabel)
        if label.isVisible()
    ]
    lock = _find(module, QToolButton, "result_history_follow_latest_button")

    assert "结果历史" not in visible_labels
    assert "自动跟随最新" not in visible_labels
    assert "已解锁" not in visible_labels
    assert visible_labels.count("结果摘要") == 1
    assert visible_labels.count("进行中") == 1
    assert "结果摘要：进行中" not in visible_labels
    assert lock.text() == "🔒"


def test_history_module_is_360_logical_pixels_and_vertically_expanding(qapp):
    module = ResultHistoryModule()

    assert module.minimumWidth() == 360
    assert module.maximumWidth() == 360
    assert module.sizePolicy().verticalPolicy() is QSizePolicy.Policy.Expanding


def test_history_module_renders_active_section_before_terminal_section(qapp):
    module = ResultHistoryModule()
    module.render_snapshot(
        _snapshot(
            active_cards=(_card("active", active=True),),
            terminal_cards=(_card("terminal"),),
        )
    )
    active_section = _find(
        module, QWidget, "result_history_active_section"
    )
    terminal_section = _find(
        module, QWidget, "result_history_terminal_section"
    )

    assert active_section.parentWidget() is terminal_section.parentWidget()
    content_layout = active_section.parentWidget().layout()
    assert content_layout.indexOf(active_section) < content_layout.indexOf(
        terminal_section
    )


def test_terminal_section_shows_no_results_even_when_active_cards_exist(qapp):
    module = ResultHistoryModule()
    module.render_snapshot(
        _snapshot(active_cards=(_card("active", active=True),))
    )

    empty = _find(module, QLabel, "result_history_terminal_empty")
    assert empty.text() == "暂无结果"
    assert not empty.isHidden()


def test_six_terminal_cards_are_scrollable(qapp):
    module = ResultHistoryModule()
    cards = tuple(_card(f"terminal-{i}", index=i) for i in range(6))
    module.render_snapshot(_snapshot(terminal_cards=cards))
    module.resize(360, 260)
    module.show()
    qapp.processEvents()

    scroll = _find(module, QScrollArea, "result_history_scroll")
    assert len(module.findChildren(ResultHistoryCard)) == 6
    assert scroll.verticalScrollBar().maximum() > 0


def test_reused_long_result_card_refits_when_scrollbar_narrows_it(qapp):
    long_ai_result = "FANS FAN\nFANS FAN\nFANS FAN"
    payload = ReviewDisplayPayload(
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
    module = ResultHistoryModule(
        payload_provider=(
            lambda run_id: payload if run_id == "target" else None
        )
    )
    target = _card("target")
    module.render_snapshot(
        _snapshot(
            terminal_cards=(target,),
            follow_latest=False,
            selected_run_id=target.run_id,
        )
    )
    module.resize(360, 500)
    module.show()
    qapp.processEvents()
    qapp.processEvents()

    scroll = _find(module, QScrollArea, "result_history_scroll")
    target_card = _card_for(module, target.run_id)
    table = _find(
        target_card,
        QTableWidget,
        "result_history_card_summary_table",
    )
    initial_result_width = table.columnWidth(3)
    initial_row_height = table.rowHeight(5)
    assert scroll.verticalScrollBar().maximum() == 0

    additional = tuple(
        _card(f"terminal-{index}", index=index)
        for index in range(1, 6)
    )
    module.render_snapshot(
        _snapshot(
            terminal_cards=(target, *additional),
            follow_latest=False,
            selected_run_id=target.run_id,
            revision=2,
        )
    )
    qapp.processEvents()
    qapp.processEvents()

    current_card = _card_for(module, target.run_id)
    available_width = max(1, table.columnWidth(3) - 12)
    required_height = max(
        23,
        table.fontMetrics()
        .boundingRect(
            QRect(0, 0, available_width, 10_000),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            long_ai_result,
        )
        .height()
        + 8,
    )

    assert current_card is target_card
    assert scroll.verticalScrollBar().maximum() > 0
    assert table.columnWidth(3) < initial_result_width
    assert required_height > initial_row_height
    assert table.rowHeight(5) >= required_height
    assert (
        sum(table.rowHeight(row) for row in range(6))
        <= table.viewport().height()
    )
    assert not table.horizontalScrollBar().isVisible()
    assert not table.verticalScrollBar().isVisible()


def test_lock_control_is_outside_scroll_at_bottom_right(qapp):
    module = ResultHistoryModule()
    module.resize(360, 420)
    module.show()
    qapp.processEvents()
    scroll = _find(module, QScrollArea, "result_history_scroll")
    lock = _find(module, QToolButton, "result_history_follow_latest_button")
    lock_position = lock.mapTo(module, QPoint(0, 0))

    assert not scroll.isAncestorOf(lock)
    assert lock_position.y() >= scroll.geometry().bottom()
    assert lock_position.x() + lock.width() >= module.width() - 12


def test_auto_follow_cards_still_emit_selection_and_scroll_remains_enabled(qapp):
    module = ResultHistoryModule()
    cards = tuple(_card(f"terminal-{i}", index=i) for i in range(6))
    module.render_snapshot(_snapshot(terminal_cards=cards))
    selected: list[str] = []
    module.run_selected.connect(selected.append)
    module.resize(360, 260)
    module.show()
    qapp.processEvents()

    QTest.mouseClick(
        _card_for(module, "terminal-1"),
        Qt.MouseButton.LeftButton,
    )

    scroll = _find(module, QScrollArea, "result_history_scroll")
    assert selected == ["terminal-1"]
    assert scroll.isEnabled()
    assert scroll.verticalScrollBar().isEnabled()


def test_unlock_emits_follow_latest_false_and_enables_selection(qapp):
    module = ResultHistoryModule()
    module.render_snapshot(_snapshot(terminal_cards=(_card("terminal"),)))
    follow_requests: list[bool] = []
    selected: list[str] = []
    module.follow_latest_requested.connect(follow_requests.append)
    module.run_selected.connect(selected.append)
    lock = _find(module, QToolButton, "result_history_follow_latest_button")
    module.show()
    qapp.processEvents()

    QTest.mouseClick(lock, Qt.MouseButton.LeftButton)
    QTest.mouseClick(
        _card_for(module, "terminal"),
        Qt.MouseButton.LeftButton,
    )

    assert follow_requests == [False]
    assert selected == ["terminal"]
    assert lock.text() == "🔓"
    assert "已解锁" not in lock.text()


def test_relock_emits_follow_latest_true_and_updates_icon_accessibility(qapp):
    module = ResultHistoryModule()
    module.render_snapshot(_snapshot(terminal_cards=(_card("terminal"),)))
    follow_requests: list[bool] = []
    module.follow_latest_requested.connect(follow_requests.append)
    lock = _find(module, QToolButton, "result_history_follow_latest_button")
    module.show()
    qapp.processEvents()

    QTest.mouseClick(lock, Qt.MouseButton.LeftButton)
    QTest.mouseClick(lock, Qt.MouseButton.LeftButton)

    assert follow_requests == [False, True]
    assert lock.text() == "🔒"
    assert lock.accessibleName() == "结果历史锁定状态：已锁定"
    assert lock.toolTip() == (
        "自动跟随：仍可查看和标注历史结果；新结果到达时返回最新"
    )


def test_transition_notice_is_visible_and_exact(qapp):
    module = ResultHistoryModule()
    snapshot = _snapshot(
        terminal_cards=(_card("remaining"),),
        follow_latest=False,
        selected_run_id="remaining",
    )
    transition = ResultHistoryTransition(
        snapshot=snapshot,
        evicted_run_id="evicted",
        selection_changed=True,
        notice="所查看结果已移出缓存",
        changed=True,
    )

    module.render_transition(transition)

    notice = _find(module, QLabel, "result_history_notice")
    assert notice.text() == "所查看结果已移出缓存"
    assert not notice.isHidden()


def test_render_reuses_or_replaces_cards_without_leaking_widgets(qapp):
    module = ResultHistoryModule()
    module.render_snapshot(_snapshot(terminal_cards=(_card("old"),)))
    old = _card_for(module, "old")

    module.render_snapshot(
        _snapshot(
            terminal_cards=(_card("new", index=2),),
            revision=2,
        )
    )
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()

    current_cards = module.findChildren(ResultHistoryCard)
    assert [card.property("run_id") for card in current_cards] == ["new"]
    assert sip.isdeleted(old)


def test_selection_only_preserves_scroll_and_relock_reveals_latest(qapp):
    module = ResultHistoryModule()
    cards = tuple(_card(f"terminal-{index}", index=index) for index in range(6))
    initial = _snapshot(
        terminal_cards=cards,
        follow_latest=False,
        selected_run_id="terminal-0",
    )
    module.render_snapshot(initial)
    module.resize(360, 260)
    module.show()
    qapp.processEvents()
    scroll = _find(module, QScrollArea, "result_history_scroll")
    scroll.verticalScrollBar().setValue(
        scroll.verticalScrollBar().maximum() // 2
    )
    initial_scroll = scroll.verticalScrollBar().value()
    identities = {
        card.property("run_id"): card
        for card in module.findChildren(ResultHistoryCard)
    }

    module.render_snapshot(initial)
    qapp.processEvents()
    assert {
        card.property("run_id"): card
        for card in module.findChildren(ResultHistoryCard)
    } == identities
    assert scroll.verticalScrollBar().value() == initial_scroll

    selection_only = _snapshot(
        terminal_cards=cards,
        follow_latest=False,
        selected_run_id="terminal-1",
        revision=2,
    )
    module.render_snapshot(selection_only)
    qapp.processEvents()

    assert {
        card.property("run_id"): card
        for card in module.findChildren(ResultHistoryCard)
    } == identities
    assert scroll.verticalScrollBar().value() == initial_scroll
    assert _card_for(module, "terminal-1").property("selected") is True

    lock_only = _snapshot(
        terminal_cards=cards,
        follow_latest=True,
        revision=3,
    )
    module.render_snapshot(lock_only)
    qapp.processEvents()

    assert {
        card.property("run_id"): card
        for card in module.findChildren(ResultHistoryCard)
    } == identities
    latest = _card_for(module, "terminal-0")
    latest_top = latest.mapTo(scroll.viewport(), QPoint(0, 0)).y()
    assert latest.property("selected") is True
    assert latest_top < scroll.viewport().height()
    assert latest_top + latest.height() > 0


def test_changed_snapshot_updates_existing_card_and_preserves_scroll(qapp):
    module = ResultHistoryModule()
    cards = tuple(_card(f"terminal-{index}", index=index) for index in range(6))
    module.render_snapshot(
        _snapshot(
            terminal_cards=cards,
            follow_latest=False,
            selected_run_id="terminal-0",
        )
    )
    module.resize(360, 260)
    module.show()
    qapp.processEvents()
    scroll = _find(module, QScrollArea, "result_history_scroll")
    scroll.verticalScrollBar().setValue(
        scroll.verticalScrollBar().maximum() // 2
    )
    initial_scroll = scroll.verticalScrollBar().value()
    identities = {
        card.property("run_id"): card
        for card in module.findChildren(ResultHistoryCard)
    }
    changed = replace(
        cards[2],
        reason="重新判定",
        revision=cards[2].revision + 1,
    )

    module.render_snapshot(
        _snapshot(
            terminal_cards=(
                cards[0],
                cards[1],
                changed,
                *cards[3:],
            ),
            follow_latest=False,
            selected_run_id="terminal-0",
            revision=2,
        )
    )
    qapp.processEvents()
    current = {
        card.property("run_id"): card
        for card in module.findChildren(ResultHistoryCard)
        if not card.isHidden()
    }

    for run_id in (
        "terminal-0",
        "terminal-1",
        "terminal-2",
        "terminal-3",
        "terminal-4",
        "terminal-5",
    ):
        assert current[run_id] is identities[run_id]
    assert scroll.verticalScrollBar().value() == initial_scroll


def test_recording_to_analysis_phases_update_one_card_without_geometry_jitter(
    qapp,
):
    module = ResultHistoryModule()
    recording = replace(
        _card("active", active=True, index=1),
        phase=ResultPhase.RECORDING,
    )
    module.render_snapshot(_snapshot(active_cards=(recording,)))
    module.resize(360, 400)
    module.show()
    QTest.qWait(30)
    qapp.processEvents()
    card = _card_for(module, "active")
    initial_geometry = (
        card.height(),
        module._scroll_content.height(),
        card.manual_judgment_slot.height(),
    )

    current = recording
    for revision, phase in enumerate(
        (
            ResultPhase.WAITING_SAVE,
            ResultPhase.WAITING_ANALYSIS,
            ResultPhase.ANALYZING,
        ),
        start=2,
    ):
        current = replace(current, phase=phase, revision=revision)
        module.render_snapshot(
            _snapshot(
                active_cards=(current,),
                revision=revision,
            )
        )
        qapp.processEvents()

        assert _card_for(module, "active") is card
        assert (
            card.height(),
            module._scroll_content.height(),
            card.manual_judgment_slot.height(),
        ) == initial_geometry


def test_auto_follow_keeps_position_for_label_update_then_scrolls_to_new_result(
    qapp,
):
    module = ResultHistoryModule()
    cards = tuple(_card(f"terminal-{index}", index=index) for index in range(6))
    active_a = _card("active-a", active=True, index=20)
    active_b = _card("active-b", active=True, index=21)
    initial = ResultHistorySnapshot(
        revision=1,
        follow_latest=True,
        selected_run_id="terminal-4",
        active_cards=(active_b, active_a),
        terminal_cards=cards,
    )
    module.render_snapshot(initial)
    module.resize(360, 260)
    module.show()
    qapp.processEvents()
    scroll = _find(
        module,
        QScrollArea,
        "result_history_scroll",
    )
    scrollbar = scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    old_scroll = scrollbar.value()
    assert old_scroll > 0

    changed = replace(
        cards[4],
        conclusion=ResultConclusion.NG,
        reason="人工标注",
        revision=cards[4].revision + 1,
    )
    replacement_snapshot = ResultHistorySnapshot(
        revision=2,
        follow_latest=True,
        selected_run_id="terminal-4",
        active_cards=(active_b, active_a),
        terminal_cards=(*cards[:4], changed, cards[5]),
    )
    module.render_transition(
        ResultHistoryTransition(
            snapshot=replacement_snapshot,
            changed=True,
        )
    )
    qapp.processEvents()

    assert scrollbar.value() == old_scroll

    finished_b = _card("active-b", index=21)
    module.render_transition(
        ResultHistoryTransition(
            snapshot=ResultHistorySnapshot(
                revision=3,
                follow_latest=True,
                selected_run_id="active-b",
                active_cards=(active_a,),
                terminal_cards=(
                    finished_b,
                    *replacement_snapshot.terminal_cards[:5],
                ),
            ),
            selection_changed=True,
            changed=True,
        )
    )
    qapp.processEvents()

    selected_card = _card_for(module, "active-b")
    selected_top = selected_card.mapTo(
        scroll.viewport(),
        QPoint(0, 0),
    ).y()
    assert selected_top < scroll.viewport().height()
    assert selected_top + selected_card.height() > 0
    result_scroll = scrollbar.value()
    assert result_scroll < old_scroll

    replaced_b = replace(
        finished_b,
        conclusion=ResultConclusion.NG,
        reason="人工标注",
        revision=finished_b.revision + 1,
    )
    module.render_transition(
        ResultHistoryTransition(
            snapshot=ResultHistorySnapshot(
                revision=4,
                follow_latest=True,
                selected_run_id="active-b",
                active_cards=(active_a,),
                terminal_cards=(
                    replaced_b,
                    *replacement_snapshot.terminal_cards[:5],
                ),
            ),
            changed=True,
        )
    )
    qapp.processEvents()

    assert scrollbar.value() == result_scroll


def test_synchronous_render_bursts_never_detach_cards_as_top_levels(qapp):
    module = ResultHistoryModule()
    cards = tuple(_card(f"terminal-{index}", index=index) for index in range(6))
    module.resize(360, 260)
    module.show()
    previous_cards: list[ResultHistoryCard] = []

    for revision in range(100):
        module.render_snapshot(
            _snapshot(
                terminal_cards=cards,
                follow_latest=False,
                selected_run_id=f"terminal-{revision % 2}",
                revision=revision,
            )
        )
        assert all(
            card.parentWidget() is not None and module.isAncestorOf(card)
            for card in previous_cards
            if not sip.isdeleted(card)
        )
        assert not any(
            isinstance(widget, ResultHistoryCard)
            for widget in qapp.topLevelWidgets()
        )
        previous_cards = module.findChildren(ResultHistoryCard)


def test_removed_card_keeps_safe_child_owner_until_deferred_delete(qapp):
    module = ResultHistoryModule()
    module.render_snapshot(_snapshot(terminal_cards=(_card("old"),)))
    old = _card_for(module, "old")
    original_parent = old.parentWidget()

    module.render_snapshot(_snapshot(revision=2))

    assert old.isHidden()
    assert old.parentWidget() is original_parent
    assert module.isAncestorOf(old)
    assert old not in qapp.topLevelWidgets()

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert sip.isdeleted(old)


def test_manual_actions_attach_only_to_focused_card_and_then_stay_stable(qapp):
    actions = QWidget()
    action_layout = QVBoxLayout(actions)
    action_button = QPushButton("manual action", actions)
    action_layout.addWidget(action_button)
    module = ResultHistoryModule(manual_judgment_widget=actions)
    cards = tuple(_card(f"terminal-{index}", index=index) for index in range(6))
    module.render_snapshot(
        _snapshot(
            terminal_cards=cards,
            follow_latest=False,
            selected_run_id="terminal-0",
        )
    )
    module.resize(360, 260)
    module.show()
    qapp.processEvents()
    scroll = _find(module, QScrollArea, "result_history_scroll")
    scrollbar = scroll.verticalScrollBar()
    card_widgets = [_card_for(module, card.run_id) for card in cards]
    QTest.qWait(180)
    qapp.processEvents()
    scrollbar.setValue(scrollbar.maximum() // 2)
    qapp.processEvents()

    assert actions.parentWidget() is card_widgets[0].manual_judgment_slot
    assert actions.isVisible()
    assert actions.isEnabled()
    assert card_widgets[0].manual_judgment_widget is actions
    first_slot = card_widgets[0].manual_judgment_slot
    assert first_slot.height() >= actions.sizeHint().height()
    assert all(
        card.manual_judgment_slot.height() == 0
        for card in card_widgets[1:]
    )

    module.render_snapshot(
        _snapshot(
            terminal_cards=cards,
            follow_latest=False,
            selected_run_id="terminal-4",
            revision=2,
        )
    )
    QTest.qWait(180)
    qapp.processEvents()

    assert card_widgets[4].manual_judgment_widget is actions
    focused_slot = card_widgets[4].manual_judgment_slot
    assert actions.parentWidget() is focused_slot
    assert focused_slot.height() >= actions.sizeHint().height()
    assert first_slot.height() == 0
    assert all(
        card.manual_judgment_widget is None
        for index, card in enumerate(card_widgets)
        if index != 4
    )
    assert action_button.isVisible()
    assert action_button.focusPolicy() is not Qt.FocusPolicy.NoFocus

    stable_geometry = (
        tuple(
            (
                card.height(),
                card.mapTo(module._scroll_content, QPoint(0, 0)).y(),
            )
            for card in card_widgets
        ),
        module._scroll_content.height(),
        scrollbar.maximum(),
    )
    module.render_snapshot(
        _snapshot(
            terminal_cards=cards,
            follow_latest=False,
            selected_run_id="terminal-4",
            revision=3,
        )
    )
    qapp.processEvents()
    assert (
        tuple(
            (
                card.height(),
                card.mapTo(module._scroll_content, QPoint(0, 0)).y(),
            )
            for card in card_widgets
        ),
        module._scroll_content.height(),
        scrollbar.maximum(),
    ) == stable_geometry

    clicked_run_ids = []
    action_button.clicked.connect(
        lambda: clicked_run_ids.extend(
            str(card.property("run_id"))
            for card in card_widgets
            if card.manual_judgment_widget is actions
        )
    )
    scrollbar.setValue(0)
    qapp.processEvents()
    scroll.ensureWidgetVisible(card_widgets[4], 0, 0)
    qapp.processEvents()

    assert actions.parentWidget() is focused_slot
    action_button.click()
    assert clicked_run_ids == ["terminal-4"]


def test_manual_actions_hide_without_legal_selection(qapp):
    actions = QWidget()
    module = ResultHistoryModule(manual_judgment_widget=actions)
    allowed = _card("allowed", index=1)
    forbidden = replace(
        _card("forbidden", index=2),
        manual_judgment_allowed=False,
    )
    module.render_snapshot(
        _snapshot(
            terminal_cards=(allowed, forbidden),
            follow_latest=False,
            selected_run_id="allowed",
        )
    )
    assert actions.parentWidget() is _card_for(
        module,
        "allowed",
    ).manual_judgment_slot

    module.render_snapshot(
        _snapshot(
            terminal_cards=(allowed, forbidden),
            follow_latest=False,
            selected_run_id="forbidden",
            revision=2,
        )
    )

    assert actions.parentWidget() is module
    assert actions.isHidden()
    assert not actions.isEnabled()


def test_long_manual_fault_text_keeps_card_height_and_preserves_scroll(qapp):
    actions = QWidget()
    action_layout = QVBoxLayout(actions)
    action_layout.addWidget(QPushButton("manual action", actions))
    module = ResultHistoryModule(manual_judgment_widget=actions)
    long_fault_type = "异" * 60
    cards = list(
        _card(f"terminal-{index}", index=index)
        for index in range(6)
    )
    target_index = 2
    cards[target_index] = replace(
        cards[target_index],
        config=replace(
            cards[target_index].config,
            fault_types=(long_fault_type,),
        ),
    )
    module.render_snapshot(
        _snapshot(
            terminal_cards=tuple(cards),
            follow_latest=False,
            selected_run_id=cards[target_index].run_id,
        )
    )
    module.resize(360, 260)
    module.show()
    QTest.qWait(180)
    qapp.processEvents()
    scroll = _find(module, QScrollArea, "result_history_scroll")
    scrollbar = scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    qapp.processEvents()
    card_widgets = [_card_for(module, card.run_id) for card in cards]
    target_card = card_widgets[target_index]
    table = _find(
        target_card,
        QTableWidget,
        "result_history_card_summary_table",
    )

    geometry_before = (
        table.height(),
        target_card.height(),
        target_card.manual_judgment_slot.height(),
        tuple(
            card.mapTo(module._scroll_content, QPoint(0, 0)).y()
            for card in card_widgets[target_index + 1 :]
        ),
        module._scroll_content.height(),
        scrollbar.maximum(),
        scrollbar.value(),
    )
    updated = replace(
        cards[target_index],
        conclusion=ResultConclusion.NG,
        fault_type=long_fault_type,
        revision=cards[target_index].revision + 1,
        manual_judgment_revision=1,
    )

    module.render_snapshot(
        _snapshot(
            terminal_cards=(
                *cards[:target_index],
                updated,
                *cards[target_index + 1 :],
            ),
            follow_latest=False,
            selected_run_id=updated.run_id,
            revision=2,
        )
    )
    QTest.qWait(20)
    qapp.processEvents()

    current_target = _card_for(module, updated.run_id)
    geometry_after = (
        table.height(),
        current_target.height(),
        current_target.manual_judgment_slot.height(),
        tuple(
            _card_for(module, card.run_id)
            .mapTo(module._scroll_content, QPoint(0, 0))
            .y()
            for card in cards[target_index + 1 :]
        ),
        module._scroll_content.height(),
        scrollbar.maximum(),
        scrollbar.value(),
    )

    assert current_target is target_card
    assert actions.parentWidget() is current_target.manual_judgment_slot
    assert geometry_after[0] == geometry_before[0]
    assert geometry_after[1] == geometry_before[1]
    assert geometry_after[2] == geometry_before[2]
    assert geometry_after[3] == geometry_before[3]
    assert geometry_after[4] == geometry_before[4]
    assert geometry_after[5] == geometry_before[5]
    assert geometry_after[6] == geometry_before[6]
