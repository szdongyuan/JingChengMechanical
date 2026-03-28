from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from app.service.analysis.frames import StftFrame
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
import app.main_window as main_window_mod


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.status = _SignalStub()
        self.log = _SignalStub()
        self.result_changed = _SignalStub()
        self.stft_updated = _SignalStub()
        self.fft_updated = _SignalStub()
        self.spl_updated = _SignalStub()
        self.octave_updated = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()


class _StubModule(QWidget):
    def __init__(self, services=None):
        super().__init__()
        self.services = services


class _ChannelProbeModule(_StubModule):
    def __init__(self, services=None):
        super().__init__(services)
        self.selected_display_channel_provider = None
        self.state_seen_channels = []
        self.review_seen_channels = []

        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)

    def _record_current_channel(self, bucket: list[int | None]) -> None:
        provider = self.selected_display_channel_provider
        bucket.append(provider() if callable(provider) else None)

    def _on_factory_state_changed(self, _state) -> None:
        self._record_current_channel(self.state_seen_channels)

    def _on_factory_review_changed(self, _review) -> None:
        self._record_current_channel(self.review_seen_channels)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _patch_non_target_modules(monkeypatch):
    for name in (
        "WorkOrderModule",
        "ControlModule",
        "VideoModule",
        "LogModule",
        "HardwareModule",
        "InputCalibrationModule",
        "ConfigModule",
        "ChannelSelectModule",
    ):
        monkeypatch.setattr(main_window_mod, name, _StubModule)


def _make_services(
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
):
    bus = _BusStub()
    factory_mode = SimpleNamespace(
        state=initial_state or FactoryControllerState(),
        review_payload=initial_review_payload or FactoryReviewPayload(),
        begin_reanalysis=MagicMock(return_value=True),
    )
    return SimpleNamespace(
        bus=bus,
        db=SimpleNamespace(close=MagicMock()),
        config_service=SimpleNamespace(
            load_config=MagicMock(return_value={"selected_channels": []})
        ),
        factory_mode=factory_mode,
    )


def _make_window(
    monkeypatch,
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(
        initial_state=initial_state,
        initial_review_payload=initial_review_payload,
    )
    return main_window_mod.MainWindow(services), services


def _show(window, qapp):
    window.resize(1440, 900)
    window.show()
    qapp.processEvents()


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


def _chart_payload_for_channels(channels: tuple[int, ...]) -> dict:
    payload: dict[int, dict] = {}
    for channel_index in channels:
        payload[channel_index] = {
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
            },
        }
    return payload


def _review_payload_with_chart_payload(
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
        no_result=False,
        error_message=None,
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload=_chart_payload_for_channels(channels),
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
    assert window.spectrum.plot.isHidden() is False
    assert window.spl.plot.isHidden() is False
    assert window.octave.plot.isHidden() is False
    assert window.spectrum.has_committed_plot_data() is False
    assert window.spl.has_committed_plot_data() is False
    assert window.octave.has_committed_plot_data() is False


def _assert_chart_cards_visible(window) -> None:
    assert window.spectrum.plot.isHidden() is False
    assert window.spl.plot.isHidden() is False
    assert window.octave.plot.isHidden() is False
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


def _emit_review_ready(
    services,
    *,
    config_id: int,
    run_id: str,
    channels: tuple[int, ...] = (0, 1),
) -> None:
    services.bus.factory_state_changed.emit(_reviewing_state(config_id))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            config_id,
            run_id=run_id,
            channels=channels,
        )
    )


def _assert_committed_chart_channel(window, *, channel_index: int) -> None:
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
    assert window.octave.current_render_channel() == channel_index
    assert window.octave.current_plot_payload()["levels_db"] == [
        32.0 + channel_index,
        34.0 + channel_index,
    ]


def _result_table_value(window, row: int, column: int) -> str:
    item = window.result.table.item(row, column)
    assert item is not None
    return item.text()


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


def test_armed_page_keeps_chart_cards_empty_until_committed_review_payload_arrives(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_armed_state(),
    )
    _show(window, qapp)

    _assert_run_specific_cards_cleared(window)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_next_run_reset_clears_result_stft_and_chart_cards_before_next_review_payload_renders(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    assert window.result.empty_state.isHidden() is True
    assert window.stft.selection_box.isVisible() is True
    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("next_run_accepted")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(_armed_state())
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

    _emit_runtime_started(services, config_id=202, run_id="run-202-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_chart_frames(services, config_id=202, run_id="run-202-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_review_ready(services, config_id=202, run_id="run-202-1")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_same_config_rerun_keeps_chart_cards_empty_until_new_review_payload_arrives(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("next_run_accepted")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(_armed_state())
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

    _emit_runtime_started(services, config_id=101, run_id="run-101-2")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, run_id="run-101-1")
    )
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

    _emit_review_ready(services, config_id=101, run_id="run-101-2")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_selection_changed_reset_clears_result_stft_and_chart_cards(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    assert window.result.empty_state.isHidden() is True
    assert window.stft.selection_box.isVisible() is True
    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("selection_changed")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(
        _armed_state(
            selected_config_ids=(303, 404),
        )
    )
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)


def test_main_window_channel_toolbar_shows_for_multi_channel_running_config(qapp, monkeypatch):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.analysis_toolbar.isVisible() is True
    assert window.analysis_toolbar.channel_combo.isVisible() is True
    assert window.analysis_toolbar.channel_combo.isEnabled() is True
    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id == {101: 0}
    assert callable(window.stft.selected_display_channel_provider)
    assert callable(window.spl.selected_display_channel_provider)
    assert callable(window.spectrum.selected_display_channel_provider)
    assert callable(window.octave.selected_display_channel_provider)
    assert window.stft.selected_display_channel_provider() == 0


def test_main_window_toolbar_selection_immediately_refreshes_stft_cached_channel(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=101,
            run_id="run-101",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.0, magnitudes=(9.0, 10.0)),
    )
    qapp.processEvents()

    assert window.stft.current_render_cache_key() == (101, "run-101", 0)
    assert np.array_equal(
        window.stft.current_image_payload()["magnitude_db"],
        np.array([[1.0], [2.0]]),
    )

    _select_toolbar_channel(window, qapp, 1)

    assert window.stft.current_render_cache_key() == (101, "run-101", 1)
    assert np.array_equal(
        window.stft.current_image_payload()["magnitude_db"],
        np.array([[9.0], [10.0]]),
    )


def test_main_window_toolbar_selection_immediately_refreshes_committed_chart_modules(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    _assert_committed_chart_channel(window, channel_index=0)

    _select_toolbar_channel(window, qapp, 1)

    _assert_committed_chart_channel(window, channel_index=1)


def test_main_window_ai_partial_no_result_keeps_rule_rows_and_committed_charts_visible(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _partial_no_result_review_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert window.result.lb_overall.text() == "无结果"
    assert window.result.error_label.isHidden() is False
    assert window.result.error_label.text() == "AI timeout"
    assert _result_table_value(window, 0, 0) == "SPL"
    assert _result_table_value(window, 0, 1) == "-1 dB"
    assert _result_table_value(window, 0, 2) == "CH2"
    assert _result_table_value(window, 0, 3) == "OK"
    assert _result_table_value(window, 1, 0) == "FFT"
    assert _result_table_value(window, 1, 1) == "2 dB"
    assert _result_table_value(window, 1, 2) == "CH1"
    assert _result_table_value(window, 1, 3) == "NG"
    assert _result_table_value(window, 2, 0) == "倍频程"
    assert _result_table_value(window, 2, 1) == "-3 dB"
    assert _result_table_value(window, 2, 2) == "CH2"
    assert _result_table_value(window, 2, 3) == "OK"
    assert _result_table_value(window, 3, 0) == "AI分析"
    assert _result_table_value(window, 3, 2) == "--"
    assert _result_table_value(window, 3, 3) == "无结果"
    _assert_chart_cards_visible(window)
    _assert_committed_chart_channel(window, channel_index=1)


def test_main_window_resolves_display_channel_before_analysis_modules_handle_factory_signals(
    qapp,
    monkeypatch,
):
    for name in ("ResultModule",):
        monkeypatch.setattr(main_window_mod, name, _StubModule)
    for name in ("SpectrumModule", "StftModule", "SplModule", "OctaveModule"):
        monkeypatch.setattr(main_window_mod, name, _ChannelProbeModule)

    window, services = _make_window(
        monkeypatch,
        initial_state=_armed_state(),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.stft.state_seen_channels[-1] == 0
    assert window.spl.state_seen_channels[-1] == 0

    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 2))
    )
    qapp.processEvents()

    assert window.stft.review_seen_channels[-1] == 0
    assert window.octave.review_seen_channels[-1] == 0


def test_main_window_channel_toolbar_hides_or_disables_selector_for_single_channel_displayed_config(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.analysis_toolbar.isVisible() is True
    assert window.analysis_toolbar.current_channel_index() == 0
    assert (
        window.analysis_toolbar.channel_combo.isHidden()
        or not window.analysis_toolbar.channel_combo.isEnabled()
    )
    assert window.display_channel_by_config_id == {101: 0}


def test_main_window_remembers_selected_channel_per_config_when_switching_displayed_configs(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.side_effect = lambda config_id: {
        101: {"config_id": 101, "selected_channels": [0, 1]},
        202: {"config_id": 202, "selected_channels": [0, 2]},
    }[config_id]
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_running_state(202))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 2)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id[101] == 1
    assert window.spl.selected_display_channel_provider() == 1

    services.bus.factory_state_changed.emit(_running_state(202))
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 2
    assert window.display_channel_by_config_id[202] == 2
    assert window.spectrum.selected_display_channel_provider() == 2


def test_main_window_review_switching_displayed_configs_restores_last_selected_channel_for_charts(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.side_effect = lambda config_id: {
        101: {"config_id": 101, "selected_channels": [0, 1]},
        202: {"config_id": 202, "selected_channels": [0, 2]},
    }[config_id]
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)
    _assert_committed_chart_channel(window, channel_index=1)

    services.bus.factory_state_changed.emit(_reviewing_state(202))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(202, channels=(0, 2))
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 2)
    _assert_committed_chart_channel(window, channel_index=2)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id[101] == 1
    _assert_committed_chart_channel(window, channel_index=1)

    services.bus.factory_state_changed.emit(_reviewing_state(202))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(202, channels=(0, 2))
    )
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 2
    assert window.display_channel_by_config_id[202] == 2
    _assert_committed_chart_channel(window, channel_index=2)


def test_main_window_startup_in_review_prefers_first_selected_config_channel_when_none_is_remembered(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [1, 0],
    }

    window = main_window_mod.MainWindow(services)
    _show(window, qapp)

    assert _toolbar_channel_options(window) == [1, 0]
    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id == {101: 1}
    assert window.stft.selected_display_channel_provider() == 1


def test_main_window_startup_in_review_renders_committed_chart_modules_on_resolved_shared_channel(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [1, 0],
    }

    window = main_window_mod.MainWindow(services)
    _show(window, qapp)

    assert window.analysis_toolbar.current_channel_index() == 1
    _assert_committed_chart_channel(window, channel_index=1)


def test_main_window_review_time_selected_channel_fallback_picks_first_committed_chart_payload_channel(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 2))
    )
    qapp.processEvents()

    assert _toolbar_channel_options(window) == [0, 1]
    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id[101] == 0
    assert window.octave.selected_display_channel_provider() == 0


def test_main_window_toolbar_selection_missing_committed_channel_resynchronizes_to_fallback(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 2)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 2))
    )
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 0
    _assert_committed_chart_channel(window, channel_index=0)

    _select_toolbar_channel(window, qapp, 1)
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id[101] == 0
    _assert_committed_chart_channel(window, channel_index=0)


def test_main_window_channel_toolbar_hides_when_returning_to_armed_without_displayed_config(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    assert window.analysis_toolbar.isVisible() is True

    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(_armed_state())
    qapp.processEvents()

    assert window.analysis_toolbar.isHidden() is True
