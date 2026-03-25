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


def _assert_run_specific_cards_cleared(window) -> None:
    assert window.result.empty_state.isHidden() is False
    assert window.result.lb_empty_text.text() == "No data"
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

    _assert_run_specific_cards_cleared(window)

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

    _assert_run_specific_cards_cleared(window)

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

    _assert_run_specific_cards_cleared(window)

    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

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
