from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

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
    selected_config_ids: tuple[int, int] = (101, 202),
    manual_primary_config_id: int | None = 101,
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        manual_primary_config_id=manual_primary_config_id,
        run_phase="armed",
    )


def _running_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, int] = (101, 202),
    manual_primary_config_id: int | None = 101,
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        manual_primary_config_id=manual_primary_config_id,
        active_config_id=config_id,
        displayed_config_id=config_id,
        run_phase="running",
        trigger_source="manual",
    )


def _reviewing_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, int] = (101, 202),
    manual_primary_config_id: int | None = 101,
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        manual_primary_config_id=manual_primary_config_id,
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
        chart_payload={
            channel_index: {
                "spl": {},
                "fft": {},
                "octave": {},
            }
            for channel_index in channels
        },
    )


def _select_toolbar_channel(window, qapp, channel_index: int) -> None:
    combo = window.analysis_toolbar.channel_combo
    combo.setCurrentIndex(combo.findData(channel_index))
    qapp.processEvents()


def _assert_run_specific_cards_cleared(window, *, result_badge: str = "-") -> None:
    assert window.result.lb_overall.text() == result_badge
    assert window.result.empty_state.isHidden() is True
    assert window.result.lb_empty_text.text() == ""
    assert window.stft.selection_box.isVisible() is False
    assert window.stft.analyze_button.isVisible() is False
    assert window.spectrum.empty_state.isHidden() is False
    assert window.spl.empty_state.isHidden() is False
    assert window.octave.empty_state.isHidden() is False


def _assert_chart_cards_visible(window) -> None:
    assert window.spectrum.empty_state.isHidden() is True
    assert window.spl.empty_state.isHidden() is True
    assert window.octave.empty_state.isHidden() is True


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


def test_armed_page_hides_run_specific_cards_until_new_run_data_arrives(qapp, monkeypatch):
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

    _assert_chart_cards_visible(window)


def test_next_run_reset_clears_result_stft_and_chart_cards_before_new_data_renders(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(_review_payload(101, run_id="run-101-1"))
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

    _assert_chart_cards_visible(window)


def test_same_config_rerun_ignores_late_old_chart_frames_after_reset(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
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

    _emit_chart_frames(services, config_id=101, run_id="run-101-2")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_selection_changed_reset_clears_result_stft_and_chart_cards(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(_review_payload(101, run_id="run-101-1"))
    qapp.processEvents()

    assert window.result.empty_state.isHidden() is True
    assert window.stft.selection_box.isVisible() is True
    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("selection_changed")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(
        _armed_state(
            selected_config_ids=(303, 404),
            manual_primary_config_id=303,
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

    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id[101] == 0
    assert window.octave.selected_display_channel_provider() == 0


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
