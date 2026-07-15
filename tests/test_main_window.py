from __future__ import annotations

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtWidgets import QApplication, QWidget

try:
    from pyqtgraph.widgets.GraphicsView import GraphicsView
except Exception:  # pragma: no cover - pyqtgraph import is environment-dependent
    GraphicsView = None
else:
    _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT = GraphicsView.paintEvent
    _NOOP_GRAPHICS_VIEW_PAINT_EVENT = lambda self, event: None

from consts.style import APP_QSS
from ui.widgets.cards import Card


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _is_analysis_chart_widget(widget: QWidget) -> bool:
    return type(widget).__module__ == "ui.widgets.analysis_charts"


def _disable_updates_and_remove_events(widget: QWidget) -> None:
    widget.setUpdatesEnabled(False)
    widgets = [widget]
    try:
        widgets.extend(widget.findChildren(QWidget))
    except RuntimeError:
        pass
    for target in widgets:
        if not sip.isdeleted(target):
            target.setUpdatesEnabled(False)
            QCoreApplication.removePostedEvents(target)


def _close_plot_widget(chart_widget: QWidget) -> None:
    plot_widget = getattr(chart_widget, "plot_widget", None)
    if plot_widget is None or sip.isdeleted(plot_widget):
        return
    _disable_updates_and_remove_events(plot_widget)
    if getattr(plot_widget, "plotItem", None) is None:
        return
    try:
        plot_widget.close()
    except AttributeError:
        pass


@pytest.fixture(autouse=True)
def _close_top_level_widgets(qapp):
    if GraphicsView is not None:
        GraphicsView.paintEvent = _NOOP_GRAPHICS_VIEW_PAINT_EVENT
    yield
    chart_widgets = []
    for widget in list(qapp.topLevelWidgets()):
        if sip.isdeleted(widget):
            continue
        for child in widget.findChildren(QWidget):
            if _is_analysis_chart_widget(child):
                chart_widgets.append(child)
                _disable_updates_and_remove_events(child)
                _close_plot_widget(child)
        widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        try:
            widget.close()
        except AttributeError:
            pass
        if sip.isdeleted(widget):
            continue
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for widget in chart_widgets:
        if not sip.isdeleted(widget):
            QCoreApplication.removePostedEvents(widget)
    for _ in range(5):
        qapp.processEvents()
    gc.collect()
    for _ in range(5):
        qapp.processEvents()
    if GraphicsView is not None:
        GraphicsView.paintEvent = _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT


def _armed_state(
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        run_phase="armed",
    )


def _running_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        active_config_id=config_id,
        displayed_config_id=config_id,
        run_phase="running",
        trigger_source="manual",
    )


def _reviewing_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        displayed_config_id=config_id,
        run_phase="reviewing",
        trigger_source="manual",
    )


def _stopped_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        displayed_config_id=config_id,
        run_phase="stopped",
        trigger_source="manual",
    )


def _review_payload(config_id: int, *, run_id: str | None = None) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id or f"run-{config_id}",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=False,
        error_message=None,
        judgment_payload={
            "overall": "合格",
            "ai_label": "Normal",
            "ai_score": 0.93,
            "ai_summary": "stable waveform",
            "ai_status": "success",
            "toolchain_name": "Toolchain",
            "toolchain_version": "1.0",
            "error_summary": None,
            "artifacts_json": ["artifact.json"],
            "spl_db": 82.4,
        },
    )


def _chart_payload_for_channels(
    channels: tuple[int, ...],
    *,
    missing_loudness_channels: tuple[int, ...] = (),
) -> dict:
    payload: dict[int, dict] = {}
    for channel_index in channels:
        channel_payload = {
            "spl": {
                "times_sec": [0.0, 1.0],
                "values_db": [70.0 + channel_index, 71.0 + channel_index],
                "lower_limit_db": 60.0,
                "upper_limit_db": 80.0,
            },
            "fft": {
                "frequencies_hz": [125.0, 250.0],
                "levels_db": [40.0 + channel_index, 42.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 35.0,
                        "upper_db": 45.0,
                        "actual_db": 42.0 + channel_index,
                        "passed": True,
                    }
                ],
                "golden_mode": "off",
                "golden_overlay_levels_db": None,
                "show_zero_reference": False,
            },
            "octave": {
                "center_freqs_hz": [125.0, 250.0],
                "levels_db": [32.0 + channel_index, 34.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 30.0,
                        "upper_db": 40.0,
                        "actual_db": 34.0 + channel_index,
                        "passed": True,
                    }
                ],
                "golden_mode": "off",
                "golden_overlay_levels_db": None,
                "show_zero_reference": False,
            },
        }
        if channel_index not in missing_loudness_channels:
            channel_payload["loudness"] = {
                "times_sec": [0.0, 1.0],
                "values": [10.0 + channel_index, 12.0 + channel_index],
                "unit": "sone",
                "lower_limit": 0.0,
                "upper_limit": 60.0,
            }
        payload[channel_index] = channel_payload
    return payload


def _review_payload_with_chart_payload(
    config_id: int,
    *,
    run_id: str | None = None,
    channels: tuple[int, ...] = (0, 1),
    missing_loudness_channels: tuple[int, ...] = (),
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id or f"run-{config_id}",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=False,
        error_message=None,
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload=_chart_payload_for_channels(
            channels,
            missing_loudness_channels=missing_loudness_channels,
        ),
    )


def _review_payload_with_vibration(
    config_id: int = 101,
    *,
    vibration_channel: int = 1,
) -> FactoryReviewPayload:
    payload = _review_payload_with_chart_payload(config_id, channels=(0, 1))
    payload.chart_payload[vibration_channel]["vibration"] = {
        "channel_index": vibration_channel,
        "sample_rate": 1000.0,
        "overall_rms": 1.0,
        "overall_peak": 2.0,
        "overall_crest_factor": 2.0,
        "dominant_frequency_hz": 50.0,
        "times_sec": [0.05, 0.10],
        "rms_values": [0.8, 1.0],
        "peak_values": [1.5, 2.0],
        "crest_factors": [1.8, 2.0],
        "calibration_value": 1.0,
    }
    return dataclasses.replace(
        payload,
        config_payload={
            "config_id": config_id,
            "selected_channels": [0, 1],
            "vibration_view_channel_index": vibration_channel,
        },
    )


def _summary_payload(
    *,
    overall_status: str,
    error_message: str | None = None,
    rows: list[dict] | None = None,
) -> dict:
    overall_label = {
        "pass": "PASS",
        "fail": "FAIL",
        "no_result": "无结果",
    }[overall_status]
    return {
        "overall_status": overall_status,
        "overall_label": overall_label,
        "error_message": error_message,
        "rows": rows or [],
    }


def _partial_no_result_review_payload(
    config_id: int,
    *,
    run_id: str | None = None,
    channels: tuple[int, ...] = (0, 1),
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id or f"run-{config_id}",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=True,
        error_message="AI timeout",
        summary_payload=_summary_payload(
            overall_status="no_result",
            error_message="AI timeout",
            rows=[
                {
                    "item_key": "spl",
                    "label": "SPL",
                    "deviation_value": -1.0,
                    "deviation_display": "-1 dB",
                    "channel_index": 1,
                    "channel_label": "CH2",
                    "status": "OK",
                    "constrained": True,
                },
                {
                    "item_key": "loudness",
                    "label": "Loudness",
                    "deviation_value": -0.5,
                    "deviation_display": "-0.5 sone",
                    "channel_index": 1,
                    "channel_label": "CH2",
                    "status": "OK",
                    "constrained": True,
                },
                {
                    "item_key": "fft",
                    "label": "FFT",
                    "deviation_value": 2.0,
                    "deviation_display": "2 dB",
                    "channel_index": 0,
                    "channel_label": "CH1",
                    "status": "NG",
                    "constrained": True,
                },
                {
                    "item_key": "octave",
                    "label": "倍频程",
                    "deviation_value": -3.0,
                    "deviation_display": "-3 dB",
                    "channel_index": 1,
                    "channel_label": "CH2",
                    "status": "OK",
                    "constrained": True,
                },
                {
                    "item_key": "ai",
                    "label": "AI分析",
                    "deviation_value": None,
                    "deviation_display": "--",
                    "channel_index": None,
                    "channel_label": "--",
                    "status": "无结果",
                    "constrained": True,
                },
            ],
        ),
        chart_payload=_chart_payload_for_channels(channels),
    )


def _select_toolbar_channel(window, qapp, channel_index: int) -> None:
    combo = window.analysis_toolbar.channel_combo
    combo.setCurrentIndex(combo.findData(channel_index))
    qapp.processEvents()


def _toolbar_channel_options(window) -> list[int]:
    combo = window.analysis_toolbar.channel_combo
    return [combo.itemData(index) for index in range(combo.count())]


def _assert_run_specific_cards_cleared(window, *, result_badge: str = "-") -> None:
    assert window.result.lb_overall.text() == result_badge
    assert window.result.empty_state.isHidden() is True
    assert window.result.lb_empty_text.text() == ""
    assert window.stft.selection_box.isVisible() is False
    assert window.stft.analyze_button.isVisible() is False
    assert window.loudness.plot.isHidden() is False
    assert window.spectrum.plot.isHidden() is False
    assert window.spl.plot.isHidden() is False
    assert window.octave.plot.isHidden() is False
    assert window.loudness.has_committed_plot_data() is False
    assert window.spectrum.has_committed_plot_data() is False
    assert window.spl.has_committed_plot_data() is False
    assert window.octave.has_committed_plot_data() is False


def _assert_chart_cards_visible(window) -> None:
    assert window.loudness.plot.isHidden() is False
    assert window.spectrum.plot.isHidden() is False
    assert window.spl.plot.isHidden() is False
    assert window.octave.plot.isHidden() is False
    assert window.loudness.has_committed_plot_data() is True
    assert window.spectrum.has_committed_plot_data() is True
    assert window.spl.has_committed_plot_data() is True
    assert window.octave.has_committed_plot_data() is True


def _emit_runtime_started(services, *, config_id: int, run_id: str) -> None:
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=config_id,
            run_id=run_id,
            trigger_source="manual",
        )
    )
    services.bus.factory_state_changed.emit(_running_state(config_id))


def _emit_chart_frames(services, *, config_id: int, run_id: str) -> None:
    services.bus.fft_updated.emit(config_id, run_id, 0, object())
    services.bus.spl_updated.emit(config_id, run_id, 0, object())
    services.bus.octave_updated.emit(config_id, run_id, 0, object())


def _emit_stft_frames_for_review_channels(
    services,
    *,
    config_id: int,
    run_id: str,
    channels: tuple[int, ...],
) -> None:
    for channel_index in channels:
        for frame_index, time_sec in enumerate((0.0, 1.0, 2.0, 3.0)):
            magnitude_base = float((channel_index * 10) + (frame_index * 2) + 1)
            services.bus.stft_updated.emit(
                config_id,
                run_id,
                channel_index,
                _make_stft_frame(
                    time=time_sec,
                    magnitudes=(magnitude_base, magnitude_base + 1.0),
                ),
            )


def _emit_review_ready(
    services,
    *,
    config_id: int,
    run_id: str,
    channels: tuple[int, ...] = (0, 1),
) -> None:
    _emit_stft_frames_for_review_channels(
        services,
        config_id=config_id,
        run_id=run_id,
        channels=channels,
    )
    services.bus.factory_state_changed.emit(_reviewing_state(config_id))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            config_id,
            run_id=run_id,
            channels=channels,
        )
    )


def _assert_committed_chart_channel(window, *, channel_index: int) -> None:
    assert window.loudness.current_render_channel() == channel_index
    assert window.loudness.current_plot_payload()["values"] == [
        10.0 + channel_index,
        12.0 + channel_index,
    ]
    assert window.spl.current_render_channel() == channel_index
    assert window.spl.current_plot_payload()["y_values"] == [
        70.0 + channel_index,
        71.0 + channel_index,
    ]
    assert window.spectrum.current_render_channel() == channel_index
    assert window.spectrum.current_plot_payload()["levels_db"] == [
        40.0 + channel_index,
        42.0 + channel_index,
    ]
    assert window.spectrum.current_plot_payload()["golden_overlay_levels_db"] is None
    assert window.spectrum.current_plot_payload()["show_zero_reference"] is False
    assert window.octave.current_render_channel() == channel_index
    assert window.octave.current_plot_payload()["levels_db"] == [
        32.0 + channel_index,
        34.0 + channel_index,
    ]
    assert window.octave.current_plot_payload()["golden_overlay_levels_db"] is None
    assert window.octave.current_plot_payload()["show_zero_reference"] is False


def _result_table_value(window, row: int, column: int) -> str:
    item = window.result.table.item(row, column)
    assert item is not None
    return item.text()


def _card_title(card: QWidget) -> str:
    title = card.findChild(QLabel, "CardTitle")
    assert title is not None
    return title.text()


def _test_page(window) -> QWidget:
    return window.tab.widget(window._TEST_PAGE_INDEX)


def _test_page_left_slot_card(window, index: int) -> QWidget:
    page = _test_page(window)
    content_layout = page.layout()
    assert content_layout is not None
    left_column = content_layout.itemAt(0).widget()
    assert left_column is not None
    left_layout = left_column.layout()
    assert left_layout is not None
    card = left_layout.itemAt(index).widget()
    assert card is not None
    return card


def _test_page_grid_card(window, row: int, column: int) -> QWidget:
    page = _test_page(window)
    content_layout = page.layout()
    assert content_layout is not None
    main_panel = content_layout.itemAt(1).widget()
    assert main_panel is not None
    main_layout = main_panel.layout()
    assert main_layout is not None
    grid = main_layout.itemAt(0).layout()
    assert grid is not None
    item = grid.itemAtPosition(row, column)
    assert item is not None
    card = item.widget()
    assert card is not None
    return card


def _test_page_card_titles(window) -> list[str]:
    return [
        label.text()
        for label in _test_page(window).findChildren(QLabel, "CardTitle")
    ]


def _make_stft_frame(
    *,
    time: float,
    magnitudes: tuple[float, ...],
    frequencies: tuple[float, ...] = (100.0, 200.0),
) -> StftFrame:
    return StftFrame(
        time=time,
        frequencies=np.asarray(frequencies, dtype=float),
        magnitudes=np.asarray(magnitudes, dtype=float),
    )


def test_card_compact_dashboard_mode_reduces_layout_chrome(qapp):
    body = QWidget()
    card = Card("频谱", body)

    normal_margins = card.layout().contentsMargins()
    assert normal_margins.left() == 12
    assert normal_margins.top() == 10
    assert normal_margins.right() == 12
    assert normal_margins.bottom() == 12
    assert card.layout().spacing() == 8
    assert card.title_metric_value_label.minimumWidth() == 68
    assert card.title_metric_value_label.minimumHeight() == 26
    assert card.property("dashboardCompact") in (None, False)

    card.set_compact_dashboard_mode(True)

    compact_margins = card.layout().contentsMargins()
    assert compact_margins.left() == 4
    assert compact_margins.top() == 4
    assert compact_margins.right() == 4
    assert compact_margins.bottom() == 4
    assert card.layout().spacing() == 3
    assert card.title_metric_value_label.minimumWidth() <= 56
    assert card.title_metric_value_label.minimumHeight() <= 22
    assert card.property("dashboardCompact") is True


def test_card_title_suffix_does_not_overlap_title_at_narrow_width(qapp):
    body = QWidget()
    card = Card("时频图", body)
    card.set_title_metric("频谱最低得分：", "87.42")
    card.resize(220, 120)
    card.show()
    qapp.processEvents()

    assert card.title_label.geometry().right() < card.title_metric_widget.geometry().left()
    assert card.title_metric_widget.geometry().top() >= card.contentsRect().top()
    assert card.title_metric_widget.geometry().right() <= card.contentsRect().right()
    assert card.title_metric_widget.geometry().bottom() < body.geometry().top()
    assert card.title_metric_value_label.minimumWidth() == 68
    assert card.title_metric_value_label.minimumHeight() == 26


def test_card_title_metric_uses_score_value_box_style():
    label_style = APP_QSS.split("QLabel#CardTitleMetricLabel", 1)[1].split("}", 1)[0]
    value_style = APP_QSS.split("QLabel#CardTitleMetricValue", 1)[1].split("}", 1)[0]

    assert "font-size: 15px;" in label_style
    assert "font-size: 15px;" in value_style
    assert "border: 1px solid #cbd5e1;" in value_style


def test_dashboard_compact_card_qss_removes_card_chrome():
    style = APP_QSS.split('QFrame#Card[dashboardCompact="true"]', 1)[1].split(
        "}", 1
    )[0]
    metric_value_style = APP_QSS.split(
        'QFrame#Card[dashboardCompact="true"] QLabel#CardTitleMetricValue',
        1,
    )[1].split("}", 1)[0]

    assert "border: 0px solid transparent;" in style
    assert "border-radius: 0px;" in style
    assert "font-size: 12px;" in metric_value_style
    assert "padding: 1px 6px;" in metric_value_style


def test_card_empty_title_suffix_is_hidden_and_title_uses_row_width(qapp):
    body = QWidget()
    card = Card("时频图", body)
    card.resize(360, 120)
    card.show()
    qapp.processEvents()

    assert card.title_suffix() == ""
    assert card.title_suffix_label.isHidden()
    assert card.title_label.geometry().width() > body.geometry().width() * 0.70
