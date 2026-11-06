from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis.frames import StftFrame
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
from ui.modules.stft import StftModule


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
        self.log = _SignalStub()
        self.status = _SignalStub()
        self.stft_updated = _SignalStub()
        self.current_trace_updated = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()


def _make_module():
    bus = _BusStub()
    factory_mode = SimpleNamespace(
        state=FactoryControllerState(),
        review_payload=FactoryReviewPayload(),
        begin_reanalysis=MagicMock(return_value=True),
    )
    services = SimpleNamespace(
        bus=bus,
        factory_mode=factory_mode,
    )
    return StftModule(services), services


def _show(module, qapp):
    module.resize(640, 260)
    module.show()
    qapp.processEvents()


def _emit_reviewing(module, services, payload: FactoryReviewPayload) -> None:
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=payload.displayed_config_id,
            run_phase="reviewing",
            trigger_source=payload.trigger_source,
        )
    )
    services.bus.factory_review_changed.emit(payload)


def _emit_phase(
    services,
    payload: FactoryReviewPayload,
    *,
    run_phase: str,
) -> None:
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=payload.displayed_config_id,
            run_phase=run_phase,
            trigger_source=payload.trigger_source,
        )
    )


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


def _make_review_payload(
    *,
    displayed_config_id: int = 101,
    run_id: str | None = None,
    captured_duration_sec: float = 5.0,
    default_window: FactoryReviewWindow | None = None,
    fallback_window: FactoryReviewWindow | None = None,
    manual_reanalysis_allowed: bool = True,
    no_result: bool = False,
    error_message: str | None = None,
    judgment_payload: dict | None = None,
    summary_payload: dict | None = None,
    chart_payload: dict | None = None,
) -> FactoryReviewPayload:
    if default_window is None:
        default_window = FactoryReviewWindow(start_sec=1.0, end_sec=3.0)
    return FactoryReviewPayload(
        displayed_config_id=displayed_config_id,
        run_id=run_id or f"run-{displayed_config_id}",
        trigger_source="manual",
        captured_duration_sec=captured_duration_sec,
        default_window=default_window,
        fallback_window=fallback_window,
        manual_reanalysis_allowed=manual_reanalysis_allowed,
        no_result=no_result,
        error_message=error_message,
        judgment_payload=judgment_payload,
        summary_payload=summary_payload,
        chart_payload=chart_payload,
    )


def _seed_stft_history(
    module,
    services,
    qapp,
    *,
    displayed_config_id: int = 101,
    run_id: str | None = None,
    channel_index: int = 0,
    times: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0),
    magnitudes: tuple[tuple[float, ...], ...] | None = None,
) -> None:
    if run_id is None:
        run_id = f"run-{displayed_config_id}"
    if magnitudes is None:
        magnitudes = tuple(
            (float((index * 2) + 1), float((index * 2) + 2))
            for index in range(len(times))
        )

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=displayed_config_id,
            run_phase="running",
            trigger_source="manual",
        )
    )
    for time, magnitude_values in zip(times, magnitudes):
        services.bus.stft_updated.emit(
            displayed_config_id,
            run_id,
            channel_index,
            _make_stft_frame(time=time, magnitudes=magnitude_values),
        )
    qapp.processEvents()


def _assert_box_matches_plot_bounds(module) -> None:
    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    box_rect = module.selection_box.geometry()

    assert plot_rect is not None
    assert box_rect.left() >= plot_rect.left()
    assert box_rect.right() <= plot_rect.right()
    assert box_rect.top() == plot_rect.top()
    assert box_rect.bottom() == plot_rect.bottom()


def _assert_selection_box_matches_selected_window(
    module,
    *,
    tolerance_px: int = 1,
) -> None:
    selected_window = module.selected_window
    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    box_rect = module.selection_box.geometry()
    heatmap_rect = module.canvas.heatmap.geometry()

    assert selected_window is not None
    assert plot_rect is not None
    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert heatmap_rect.size() == module.canvas.rect().size()

    start_sec, end_sec = selected_window
    expected_left = module.canvas.heatmap.widget_x_for_time_sec(start_sec)
    expected_right = module.canvas.heatmap.widget_x_for_time_sec(end_sec)

    assert expected_left is not None
    assert expected_right is not None
    assert box_rect.left() == pytest.approx(expected_left, abs=tolerance_px)
    assert box_rect.right() == pytest.approx(expected_right, abs=tolerance_px)
    assert box_rect.top() == plot_rect.top()
    assert box_rect.bottom() == plot_rect.bottom()
