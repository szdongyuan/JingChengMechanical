from __future__ import annotations

from dataclasses import asdict
import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
from app.service.analysis import dsp
from app.service.analysis.judgment import JudgmentEngine
import ui.modules.result as result_module_mod
from ui.modules.result import ResultModule


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
        self.result_changed = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.spl_updated = _SignalStub()
        self.loudness_updated = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_module() -> ResultModule:
    services = SimpleNamespace(
        bus=_BusStub(),
        factory_mode=SimpleNamespace(
            state=FactoryControllerState(),
            review_payload=FactoryReviewPayload(),
        ),
    )
    return ResultModule(services)


def _table_value(module: ResultModule, row: int, column: int) -> str:
    item = module.table.item(row, column)
    assert item is not None
    return item.text()


def _metric_text(module: ResultModule, attr: str) -> str:
    return getattr(module, attr).text()


def _set_selected_channel(module: ResultModule, channel_index: int | None) -> None:
    module.selected_display_channel_provider = lambda: channel_index


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
        "rows": rows or [
            {
                "item_key": "spl",
                "label": "SPL",
                "deviation_value": -1.0,
                "deviation_display": "-1 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "spl_average",
                "label": "平均SPL",
                "deviation_value": -1.5,
                "deviation_display": "-1.5 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "loudness",
                "label": "Loudness",
                "deviation_value": -2.0,
                "deviation_display": "-2 sone",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "loudness_average",
                "label": "平均Loudness",
                "deviation_value": -2.5,
                "deviation_display": "-2.5 sone",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "fft",
                "label": "FFT",
                "deviation_value": -3.0,
                "deviation_display": "-3 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "octave",
                "label": "倍频程",
                "deviation_value": -4.0,
                "deviation_display": "-4 dB",
                "channel_index": 0,
                "channel_label": "CH1",
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
                "status": "OK",
                "constrained": False,
            },
        ],
    }


def _state(
    run_phase: str,
    *,
    displayed_config_id: int | None = None,
    active_config_id: int | None = None,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        active_config_id=active_config_id,
        displayed_config_id=displayed_config_id,
        run_phase=run_phase,
        trigger_source="manual" if displayed_config_id is not None else None,
    )


def _make_review_payload(
    *,
    no_result: bool = False,
    error_message: str | None = None,
    judgment_payload: dict | None = None,
    summary_payload: dict | None = None,
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=101,
        run_id="run-101",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=no_result,
        error_message=error_message,
        judgment_payload=judgment_payload,
        summary_payload=summary_payload,
    )


def _assert_placeholder(module: ResultModule, *, badge_text: str) -> None:
    assert module.lb_overall.text() == badge_text
    assert module.table.rowCount() == 7
    assert _table_value(module, 0, 0) == "声压级"
    assert _table_value(module, 1, 0) == "平均声压级"
    assert _table_value(module, 2, 0) == "响度"
    assert _table_value(module, 3, 0) == "平均响度"
    assert _table_value(module, 4, 0) == "FFT频谱"
    assert _table_value(module, 5, 0) == "倍频程"
    assert _table_value(module, 6, 0) == "AI分析"
    assert _table_value(module, 0, 1) == "-"
    assert _table_value(module, 0, 2) == "-"
    assert _table_value(module, 0, 3) == "-"
    assert getattr(module, "error_label").isHidden() is True


def test_result_module_shows_metric_placeholders_before_config(qapp):
    module = _make_module()

    assert _metric_text(module, "lb_spl_metric_label") == "声压级"
    assert module.lb_spl_metric_value.text() == "--"
    assert _metric_text(module, "lb_loudness_metric_label") == "响度"
    assert module.lb_loudness_metric_value.text() == "--"


def test_result_module_updates_live_metrics_only_for_current_run_and_channel(qapp):
    module = _make_module()
    _set_selected_channel(module, 1)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 1, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(202, "run-101", 1, SimpleNamespace(spl_db=90.0))
    module.services.bus.spl_updated.emit(101, "old-run", 1, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=71.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-101", 1, SimpleNamespace(spl_db=float("nan")))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-101", 1, SimpleNamespace(spl_db=72.34))
    module.services.bus.loudness_updated.emit(
        101, "run-101", 1, SimpleNamespace(loudness_sone=float("inf"))
    )
    assert module.lb_loudness_metric_value.text() == "--"
    module.services.bus.loudness_updated.emit(101, "run-101", 1, SimpleNamespace(loudness_sone=2.345))
    module.services.bus.spl_updated.emit(101, "old-run", 1, SimpleNamespace(spl_db=80.0))
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=9.0))

    assert module.lb_spl_metric_label.text() == "声压级"
    assert module.lb_spl_metric_value.text() == "72.3 dB"
    assert module.lb_loudness_metric_label.text() == "响度"
    assert module.lb_loudness_metric_value.text() == "2.35 sone"


def test_result_module_live_metric_frames_use_cached_config_after_refresh(qapp, monkeypatch):
    times = iter([0.0, 0.25, 0.50])
    monkeypatch.setattr(
        result_module_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times)),
        raising=False,
    )
    module = _make_module()
    _set_selected_channel(module, 0)
    load_calls: list[int] = []

    def load_config(config_id):
        load_calls.append(config_id)
        return {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
        }

    module.services.config_service = SimpleNamespace(load_config=load_config)
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.refresh_selected_display_channel()
    established_load_count = len(load_calls)
    assert established_load_count >= 1

    for value, frame_time in ((70.0, 0.0), (71.0, 0.25), (72.0, 0.50)):
        module.services.bus.spl_updated.emit(
            101,
            "run-101",
            0,
            SimpleNamespace(spl_db=value, time=frame_time),
        )
        module.services.bus.loudness_updated.emit(
            101,
            "run-101",
            0,
            SimpleNamespace(loudness_sone=2.0),
        )

    assert module.lb_spl_metric_value.text() == "72.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"
    assert len(load_calls) == established_load_count


def test_result_module_non_current_spl_frames_reuse_displayed_config_metadata(qapp):
    selected_channel = 0
    module = _make_module()
    module.selected_display_channel_provider = lambda: selected_channel
    load_calls: list[int] = []

    def load_config(config_id):
        load_calls.append(config_id)
        return {
            "config_id": 101,
            "selected_channels": [0, 1],
            "channels": [
                {"channel_index": 0, "loudness_unit": "sone"},
                {"channel_index": 1, "loudness_unit": "sone"},
            ],
        }

    module.services.config_service = SimpleNamespace(load_config=load_config)
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.refresh_selected_display_channel()
    established_load_count = len(load_calls)
    assert established_load_count >= 1

    for value, frame_time in ((71.0, 0.0), (72.0, 0.1), (73.0, 0.2)):
        module.services.bus.spl_updated.emit(
            101,
            "run-101",
            1,
            SimpleNamespace(spl_db=value, time=frame_time),
        )

    assert len(load_calls) == established_load_count
    assert module._live_spl_by_key[(101, "run-101", 1)] == pytest.approx(73.0)
    assert module.lb_spl_metric_value.text() == "--"

    selected_channel = 1
    module.refresh_selected_display_channel()

    assert module.lb_spl_metric_value.text() == "73.0 dB"


def test_result_module_throttles_only_spl_metric_repaint_while_updating_cache(
    qapp,
    monkeypatch,
):
    times = iter([0.0, 0.10, 0.25])
    monkeypatch.setattr(
        result_module_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times)),
        raising=False,
    )
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=0.0),
    )
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=62.0, time=0.10),
    )
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module._live_spl_by_key[(101, "run-101", 0)] == pytest.approx(62.0)

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=63.0, time=0.25),
    )
    assert module.lb_spl_metric_value.text() == "63.0 dB"


def test_result_module_spl_throttle_uses_finite_frame_time_over_wall_clock(
    qapp,
    monkeypatch,
):
    times = iter([5000.0, 5000.10])
    monkeypatch.setattr(
        result_module_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times)),
        raising=False,
    )
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=60.0, time=10.00),
    )
    assert module.lb_spl_metric_value.text() == "60.0 dB"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=10.25),
    )

    assert module.lb_spl_metric_value.text() == "61.0 dB"


def test_result_module_spl_throttle_uses_one_clock_after_missing_frame_time(
    qapp,
    monkeypatch,
):
    times = iter([1000.0, 1000.10, 1000.30])
    monkeypatch.setattr(
        result_module_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times)),
        raising=False,
    )
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=60.0),
    )
    assert module.lb_spl_metric_value.text() == "60.0 dB"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=0.10),
    )
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module._live_spl_by_key[(101, "run-101", 0)] == pytest.approx(61.0)

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=62.0, time=0.36),
    )
    assert module.lb_spl_metric_value.text() == "62.0 dB"


def test_result_module_spl_throttle_uses_monotonic_fallback_for_nonfinite_frame_time(
    qapp,
    monkeypatch,
):
    times = iter([10.0, 10.1, 10.3])
    monkeypatch.setattr(
        result_module_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times)),
        raising=False,
    )
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=60.0, time=float("nan")),
    )
    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=None),
    )
    assert module.lb_spl_metric_value.text() == "60.0 dB"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=62.0),
    )
    assert module.lb_spl_metric_value.text() == "62.0 dB"


def test_result_module_selected_channel_refresh_bypasses_spl_throttle(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=0.0),
    )
    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=62.0, time=0.10),
    )
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.refresh_selected_display_channel()

    assert module.lb_spl_metric_value.text() == "62.0 dB"


def test_result_module_invalid_spl_clears_repaint_timestamp_and_next_valid_repaints(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=0.0),
    )
    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=float("nan"), time=0.10),
    )
    assert module.lb_spl_metric_value.text() == "--"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=63.0, time=0.11),
    )

    assert module.lb_spl_metric_value.text() == "63.0 dB"


def test_result_module_loudness_repaints_immediately_without_forcing_spl_repaint(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=0.0),
    )
    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=62.0, time=0.10),
    )

    module.services.bus.loudness_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(loudness_sone=2.0, time=0.10),
    )

    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"


def test_result_module_matching_invalid_spl_clears_only_spl_metric(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=62.1))
    module.services.bus.loudness_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(loudness_sone=1.25),
    )
    assert module.lb_spl_metric_value.text() == "62.1 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.spl_updated.emit(101, "old-run", 0, SimpleNamespace(spl_db=float("nan")))
    module.services.bus.spl_updated.emit(101, "run-101", 1, SimpleNamespace())
    assert module.lb_spl_metric_value.text() == "62.1 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=float("nan")))
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=63.2))
    assert module.lb_spl_metric_value.text() == "63.2 dB"
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace())
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"


def test_result_module_matching_invalid_loudness_clears_only_loudness_metric(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=62.1))
    module.services.bus.loudness_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(loudness_sone=2.345),
    )
    assert module.lb_spl_metric_value.text() == "62.1 dB"
    assert module.lb_loudness_metric_value.text() == "2.35 sone"

    module.services.bus.loudness_updated.emit(
        101,
        "old-run",
        0,
        SimpleNamespace(loudness_sone=float("inf")),
    )
    module.services.bus.loudness_updated.emit(101, "run-101", 1, SimpleNamespace())
    assert module.lb_spl_metric_value.text() == "62.1 dB"
    assert module.lb_loudness_metric_value.text() == "2.35 sone"

    module.services.bus.loudness_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(loudness_sone=float("inf")),
    )
    assert module.lb_spl_metric_value.text() == "62.1 dB"
    assert module.lb_loudness_metric_value.text() == "--"

    module.services.bus.loudness_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(loudness_sone=3.0),
    )
    assert module.lb_loudness_metric_value.text() == "3.00 sone"
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace())
    assert module.lb_spl_metric_value.text() == "62.1 dB"
    assert module.lb_loudness_metric_value.text() == "--"


def test_result_module_preserves_live_metric_caches_when_selected_channel_changes(
    qapp,
    monkeypatch,
):
    times = iter([0.0, 0.0, 0.25])
    monkeypatch.setattr(
        result_module_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(times)),
        raising=False,
    )
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [
            {"channel_index": 0, "loudness_unit": "sone"},
            {"channel_index": 1, "loudness_unit": "sone"},
        ],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=61.0, time=0.0),
    )
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=1.25))
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    _set_selected_channel(module, 1)
    module.refresh_selected_display_channel()
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        1,
        SimpleNamespace(spl_db=72.34, time=0.0),
    )
    module.services.bus.loudness_updated.emit(101, "run-101", 1, SimpleNamespace(loudness_sone=2.0))
    assert module.lb_spl_metric_value.text() == "72.3 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"

    _set_selected_channel(module, 0)
    module.refresh_selected_display_channel()
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.spl_updated.emit(
        101,
        "run-101",
        0,
        SimpleNamespace(spl_db=62.0, time=0.25),
    )
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=1.5))
    assert module.lb_spl_metric_value.text() == "62.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.50 sone"


def test_result_module_renders_selected_channel_average_metrics_from_judgment_payload(qapp):
    module = _make_module()
    _set_selected_channel(module, 1)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 1, "loudness_unit": "phon"}],
    })
    judgment_payload = {
        "channel_results": [
            {"channel_index": 0, "spl_average_db": 60.0, "loudness_average_sone": 1.0},
            {"channel_index": 1, "spl_average_db": 66.66, "loudness_average_sone": 2.0},
        ]
    }

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=judgment_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_label.text() == "平均声压级"
    assert module.lb_spl_metric_value.text() == "66.7 dB"
    assert module.lb_loudness_metric_label.text() == "平均响度"
    assert module.lb_loudness_metric_value.text() == "50.0 phon"


def test_result_module_review_channel_switch_rerenders_selected_channel_average_metrics(qapp):
    selected_channel = 0
    module = _make_module()
    module.selected_display_channel_provider = lambda: selected_channel
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [
            {"channel_index": 0, "loudness_unit": "sone"},
            {"channel_index": 1, "loudness_unit": "sone"},
        ],
    })
    judgment_payload = {
        "channel_results": [
            {"channel_index": 0, "spl_average_db": 60.0, "loudness_average_sone": 1.0},
            {"channel_index": 1, "spl_average_db": 66.0, "loudness_average_sone": 2.0},
        ]
    }

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=judgment_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )
    assert module.lb_spl_metric_value.text() == "60.0 dB"

    selected_channel = 1
    module.refresh_selected_display_channel()

    assert module.lb_spl_metric_value.text() == "66.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"


def test_result_module_review_average_requires_matching_displayed_config(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    review_payload = FactoryReviewPayload(
        displayed_config_id=202,
        run_id="run-202",
        judgment_payload={
            "channel_results": [
                {"channel_index": 0, "spl_average_db": 66.0, "loudness_average_sone": 2.0},
            ]
        },
        summary_payload=_summary_payload(overall_status="pass"),
    )

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(review_payload)

    assert module.lb_spl_metric_label.text() == "平均声压级"
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_label.text() == "平均响度"
    assert module.lb_loudness_metric_value.text() == "--"


@pytest.mark.parametrize("phase", ["running", "analyzing", "reanalyzing", "armed", "idle"])
def test_result_module_review_averages_render_only_in_reviewing_or_stopped(qapp, phase):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state(phase, displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={
                "channel_results": [
                    {"channel_index": 0, "spl_average_db": 66.0, "loudness_average_sone": 2.0},
                ]
            },
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"
    assert module.lb_spl_metric_label.text() != "平均声压级"


@pytest.mark.parametrize(
    ("channel_result", "expected_spl", "expected_loudness"),
    [
        (
            {"channel_index": 1, "spl_average_db": 88.0, "loudness_average_sone": 8.0},
            "--",
            "--",
        ),
        ({"channel_index": 0, "spl_average_db": 66.0}, "66.0 dB", "--"),
        ({"channel_index": 0, "loudness_average_sone": 2.0}, "--", "2.00 sone"),
        (
            {
                "channel_index": 0,
                "spl_average_db": float("nan"),
                "loudness_average_sone": 3.0,
            },
            "--",
            "3.00 sone",
        ),
    ],
)
def test_result_module_review_average_missing_fields_are_independent(
    qapp,
    channel_result,
    expected_spl,
    expected_loudness,
):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={"channel_results": [channel_result]},
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_value.text() == expected_spl
    assert module.lb_loudness_metric_value.text() == expected_loudness


def test_result_module_renders_average_metrics_from_production_serialized_judgment_result(
    qapp,
    monkeypatch,
):
    module = _make_module()
    _set_selected_channel(module, 0)
    channel_config = {
        "channel_index": 0,
        "spl_weighting": "Z",
        "spl_time_constant": 0.125,
        "spl_lower_limit": 0.0,
        "spl_upper_limit": 200.0,
        "loudness_unit": "sone",
        "loudness_lower_limit": 0.0,
        "loudness_upper_limit": 60.0,
        "fft_band_limits": [],
        "fft_golden_mode": "off",
        "fft_show_golden_overlay": False,
        "oct_weighting": "Z",
        "oct_band_limits": [],
        "oct_golden_mode": "off",
        "oct_show_golden_overlay": False,
    }
    config = {
        "config_id": 101,
        "analysis_duration": 0.5,
        "analysis_window_offset_ms": 1000,
        "selected_channels": [0],
        "channels": [channel_config],
    }
    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace",
        lambda *args, **kwargs: ([0.0, 0.5], [1.0, 3.0], 3.0),
    )
    engine = JudgmentEngine(SimpleNamespace(resolve_binding=lambda _config_id: None))
    background_pressure = 20e-6 * 10 ** (40.0 / 20.0)
    target_pressure = 20e-6 * 10 ** (50.0 / 20.0)
    buffer = np.concatenate(
        [
            np.full((1000, 1), background_pressure, dtype=np.float64),
            np.full((1000, 1), target_pressure, dtype=np.float64),
        ]
    )
    judgment_payload = asdict(
        engine.judge(
            config,
            buffer,
            1000.0,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )
    )
    serialized_channel = judgment_payload["channel_results"][0]
    assert judgment_payload["analysis_window"] == pytest.approx((1.0, 1.5))
    assert serialized_channel["spl_average_db"] == pytest.approx(50.0, abs=0.01)
    assert serialized_channel["loudness_average_sone"] == pytest.approx(2.0)

    module.services.config_service = SimpleNamespace(load_config=lambda _cid: config)
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=judgment_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_value.text() == "50.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"


@pytest.mark.parametrize("config_service_available", [False, True])
def test_result_module_hides_average_metrics_when_displayed_config_cannot_load(
    qapp,
    config_service_available,
):
    module = _make_module()
    _set_selected_channel(module, 1)
    if config_service_available:
        def raise_load_error(_cid):
            raise RuntimeError("config unavailable")

        module.services.config_service = SimpleNamespace(load_config=raise_load_error)
    judgment_payload = {
        "channel_results": [
            {"channel_index": 1, "spl_average_db": 66.66, "loudness_average_sone": 2.0},
        ]
    }

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=judgment_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_label.text() == "平均声压级"
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_label.text() == "平均响度"
    assert module.lb_loudness_metric_value.text() == "--"


def test_result_module_hides_metrics_when_provider_channel_is_not_selected(qapp):
    module = _make_module()
    _set_selected_channel(module, 1)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 1, "loudness_unit": "phon"}],
    })

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 1, SimpleNamespace(spl_db=72.34))
    module.services.bus.loudness_updated.emit(101, "run-101", 1, SimpleNamespace(loudness_sone=2.0))

    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={
                "channel_results": [
                    {"channel_index": 1, "spl_average_db": 66.66, "loudness_average_sone": 2.0},
                ]
            },
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_label.text() == "平均声压级"
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_label.text() == "平均响度"
    assert module.lb_loudness_metric_value.text() == "--"


def test_result_module_hides_metrics_when_selected_channel_config_is_missing(qapp):
    module = _make_module()
    _set_selected_channel(module, 1)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 1, SimpleNamespace(spl_db=72.34))
    module.services.bus.loudness_updated.emit(101, "run-101", 1, SimpleNamespace(loudness_sone=2.0))

    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={
                "channel_results": [
                    {"channel_index": 1, "spl_average_db": 66.66, "loudness_average_sone": 2.0},
                ]
            },
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )

    assert module.lb_spl_metric_label.text() == "平均声压级"
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_label.text() == "平均响度"
    assert module.lb_loudness_metric_value.text() == "--"


def test_result_module_clears_metric_values_during_reanalysis_until_replacement_review(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    first_payload = {
        "channel_results": [
            {"channel_index": 0, "spl_average_db": 60.0, "loudness_average_sone": 1.5}
        ]
    }
    second_payload = {
        "channel_results": [
            {"channel_index": 0, "spl_average_db": 63.0, "loudness_average_sone": 2.5}
        ]
    }

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=first_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )
    assert module.lb_spl_metric_value.text() == "60.0 dB"

    module.services.bus.factory_state_changed.emit(_state("reanalyzing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=None, summary_payload=None)
    )
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload=second_payload,
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )
    assert module.lb_spl_metric_value.text() == "63.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.50 sone"


def test_result_module_clears_live_run_when_displayed_config_changes(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda config_id: {
        "config_id": config_id,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=61.0))
    module._live_spl_by_key[(303, "run-303", 0)] = 80.0
    module._last_spl_repaint_time_by_key[(303, "run-303", 0)] = 3.0
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=202, active_config_id=202)
    )
    assert module.lb_spl_metric_value.text() == "--"
    assert (101, "run-101", 0) not in module._live_spl_by_key
    assert (101, "run-101", 0) not in module._last_spl_repaint_time_by_key
    assert module._live_spl_by_key[(303, "run-303", 0)] == pytest.approx(80.0)
    assert module._last_spl_repaint_time_by_key[(303, "run-303", 0)] == pytest.approx(3.0)

    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"


def test_result_module_new_matching_start_clears_prior_live_metrics_and_rejects_old_run(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-1", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-1", 0, SimpleNamespace(spl_db=61.0))
    module._live_spl_by_key[(202, "run-x", 0)] = 99.0
    module._live_loudness_by_key[(202, "run-x", 0)] = 4.0
    module._last_spl_repaint_time_by_key[(202, "run-x", 0)] = 9.0
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-2", trigger_source="manual")
    )
    assert module.lb_spl_metric_value.text() == "--"
    assert (101, "run-1", 0) not in module._live_spl_by_key
    assert (101, "run-1", 0) not in module._last_spl_repaint_time_by_key
    assert module._live_spl_by_key[(202, "run-x", 0)] == pytest.approx(99.0)
    assert module._live_loudness_by_key[(202, "run-x", 0)] == pytest.approx(4.0)
    assert module._last_spl_repaint_time_by_key[(202, "run-x", 0)] == pytest.approx(9.0)

    module.services.bus.spl_updated.emit(101, "run-1", 0, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-2", 0, SimpleNamespace(spl_db=62.0))
    assert module.lb_spl_metric_value.text() == "62.0 dB"


def test_result_module_runtime_start_for_active_non_displayed_config_does_not_replace_displayed_run(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda config_id: {
        "config_id": config_id,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=202, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=202, run_id="run-202", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(202, "run-202", 0, SimpleNamespace(spl_db=61.0))
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )

    assert module._current_live_run_id == "run-202"
    assert module.lb_spl_metric_value.text() == "61.0 dB"


def test_result_module_preserves_latest_live_metrics_during_analyzing_until_review_arrives(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=61.0))
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=1.25))

    module.services.bus.factory_state_changed.emit(
        _state("analyzing", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_analyzing_entered.emit(
        SimpleNamespace(config_id=101, run_id="run-101")
    )
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={
                "channel_results": [
                    {"channel_index": 0, "spl_average_db": 64.0, "loudness_average_sone": 2.0}
                ]
            },
            summary_payload=_summary_payload(overall_status="pass"),
        )
    )
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    assert module.lb_spl_metric_value.text() == "64.0 dB"
    assert module.lb_loudness_metric_value.text() == "2.00 sone"


def test_result_module_factory_display_reset_clears_metrics_and_live_run(qapp):
    module = _make_module()
    _set_selected_channel(module, 0)
    module.services.config_service = SimpleNamespace(load_config=lambda _cid: {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [{"channel_index": 0, "loudness_unit": "sone"}],
    })
    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )
    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-101", trigger_source="manual")
    )
    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=61.0))
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=1.25))
    assert module._last_spl_repaint_time_by_key
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.factory_display_reset.emit("next_run_accepted")
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"
    assert module._live_spl_by_key == {}
    assert module._live_loudness_by_key == {}
    assert module._last_spl_repaint_time_by_key == {}

    module.services.bus.spl_updated.emit(101, "run-101", 0, SimpleNamespace(spl_db=70.0))
    module.services.bus.loudness_updated.emit(101, "run-101", 0, SimpleNamespace(loudness_sone=2.0))
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"


def test_result_module_renders_completed_summary_rows_from_summary_payload(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={"overall": "合格"},
            summary_payload=_summary_payload(
                overall_status="fail",
                rows=[
                    {
                        "item_key": "spl",
                        "label": "SPL",
                        "deviation_value": 6.0,
                        "deviation_display": "6 dB",
                        "channel_index": 1,
                        "channel_label": "CH2",
                        "status": "NG",
                        "constrained": True,
                    },
                    {
                        "item_key": "spl_average",
                        "label": "平均SPL",
                        "deviation_value": 4.0,
                        "deviation_display": "4 dB",
                        "channel_index": 1,
                        "channel_label": "CH2",
                        "status": "NG",
                        "constrained": True,
                    },
                    {
                        "item_key": "loudness",
                        "label": "Loudness",
                        "deviation_value": 4.0,
                        "deviation_display": "4 sone",
                        "channel_index": 1,
                        "channel_label": "CH2",
                        "status": "NG",
                        "constrained": True,
                    },
                    {
                        "item_key": "loudness_average",
                        "label": "平均Loudness",
                        "deviation_value": -1.0,
                        "deviation_display": "-1 sone",
                        "channel_index": 1,
                        "channel_label": "CH2",
                        "status": "OK",
                        "constrained": True,
                    },
                    {
                        "item_key": "fft",
                        "label": "FFT",
                        "deviation_value": -3.0,
                        "deviation_display": "-3 dB",
                        "channel_index": 0,
                        "channel_label": "CH1",
                        "status": "OK",
                        "constrained": True,
                    },
                    {
                        "item_key": "octave",
                        "label": "倍频程",
                        "deviation_value": 0.0,
                        "deviation_display": "0 dB",
                        "channel_index": 0,
                        "channel_label": "CH1",
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
                        "status": "OK",
                        "constrained": False,
                    },
                ],
            ),
        )
    )

    assert module.lb_overall.text() == "FAIL"
    assert module.lb_overall.property("resultLevel") == "fail"
    assert module.table.rowCount() == 7
    assert _table_value(module, 0, 0) == "声压级"
    assert _table_value(module, 0, 1) == "6 dB"
    assert _table_value(module, 0, 2) == "通道2"
    assert _table_value(module, 0, 3) == "NG"
    assert _table_value(module, 1, 0) == "平均声压级"
    assert _table_value(module, 1, 1) == "4 dB"
    assert _table_value(module, 1, 2) == "通道2"
    assert _table_value(module, 1, 3) == "NG"
    assert _table_value(module, 2, 0) == "响度"
    assert _table_value(module, 2, 1) == "4 sone"
    assert _table_value(module, 2, 2) == "通道2"
    assert _table_value(module, 2, 3) == "NG"
    assert _table_value(module, 3, 0) == "平均响度"
    assert _table_value(module, 3, 1) == "-1 sone"
    assert _table_value(module, 3, 2) == "通道2"
    assert _table_value(module, 3, 3) == "OK"
    assert _table_value(module, 4, 0) == "FFT频谱"
    assert _table_value(module, 4, 1) == "-3 dB"
    assert _table_value(module, 4, 2) == "通道1"
    assert _table_value(module, 4, 3) == "OK"
    assert _table_value(module, 5, 0) == "倍频程"
    assert _table_value(module, 5, 1) == "0 dB"
    assert _table_value(module, 5, 2) == "通道1"
    assert _table_value(module, 6, 0) == "AI分析"
    assert _table_value(module, 6, 1) == "--"
    assert _table_value(module, 6, 2) == "--"
    assert _table_value(module, 6, 3) == "OK"


def test_result_module_preserves_approved_status_labels(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=_summary_payload(overall_status="fail"))
    )

    assert module.lb_overall.text() == "FAIL"
    assert _table_value(module, 0, 3) == "OK"


def test_result_module_translates_average_row_labels_at_ui_boundary(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    payload = _summary_payload(overall_status="pass")
    payload["rows"][0]["label"] = "平均SPL"
    payload["rows"][1]["label"] = "平均Loudness"

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=payload)
    )

    assert _table_value(module, 0, 0) == "平均声压级"
    assert _table_value(module, 1, 0) == "平均响度"


def test_result_module_translates_english_octave_row_label(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    payload = _summary_payload(overall_status="pass")
    payload["rows"][5]["label"] = "Octave"

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=payload)
    )

    assert _table_value(module, 5, 0) == "倍频程"


@pytest.mark.parametrize("phase", ["analyzing", "reanalyzing"])
def test_result_module_shows_analysis_in_progress_when_summary_payload_is_missing(qapp, phase):
    module = _make_module()

    module.services.bus.factory_state_changed.emit(
        _state(
            phase,
            displayed_config_id=101,
            active_config_id=101 if phase == "analyzing" else None,
        )
    )
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            judgment_payload={"overall": "合格"},
            summary_payload=None,
        )
    )

    _assert_placeholder(module, badge_text="分析中")


def test_result_module_keeps_in_progress_badge_when_plain_stopped_preserves_display(qapp):
    module = _make_module()

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=101, active_config_id=101)
    )

    assert module.lb_overall.text() == "分析中"

    module.services.bus.factory_state_changed.emit(_state("stopped", displayed_config_id=101))

    _assert_placeholder(module, badge_text="分析中")


def test_result_module_preserves_existing_summary_card_when_plain_stop_enters_stopped(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=_summary_payload(overall_status="pass"))
    )

    assert module.lb_overall.text() == "PASS"

    module.services.bus.factory_state_changed.emit(_state("stopped", displayed_config_id=101))

    assert module.lb_overall.text() == "PASS"
    assert module.lb_overall.property("resultLevel") == "pass"
    assert _table_value(module, 0, 0) == "声压级"
    assert _table_value(module, 6, 0) == "AI分析"


def test_result_module_shows_no_result_plus_error_text_from_summary_payload(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            no_result=True,
            error_message="MoSQITo unavailable",
            judgment_payload={"overall": "合格"},
            summary_payload=_summary_payload(
                overall_status="no_result",
                error_message="MoSQITo unavailable",
                rows=[
                    {
                        "item_key": "spl",
                        "label": "SPL",
                        "deviation_value": -1.0,
                        "deviation_display": "-1 dB",
                        "channel_index": 0,
                        "channel_label": "CH1",
                        "status": "OK",
                        "constrained": True,
                    },
                    {
                        "item_key": "spl_average",
                        "label": "平均SPL",
                        "deviation_value": -1.0,
                        "deviation_display": "-1 dB",
                        "channel_index": 0,
                        "channel_label": "CH1",
                        "status": "OK",
                        "constrained": True,
                    },
                    {
                        "item_key": "loudness",
                        "label": "Loudness",
                        "deviation_value": None,
                        "deviation_display": "--",
                        "channel_index": 1,
                        "channel_label": "CH2",
                        "status": "无结果",
                        "constrained": True,
                    },
                    {
                        "item_key": "loudness_average",
                        "label": "平均Loudness",
                        "deviation_value": None,
                        "deviation_display": "--",
                        "channel_index": 1,
                        "channel_label": "CH2",
                        "status": "无结果",
                        "constrained": True,
                    },
                    {
                        "item_key": "fft",
                        "label": "FFT",
                        "deviation_value": -2.0,
                        "deviation_display": "-2 dB",
                        "channel_index": 0,
                        "channel_label": "CH1",
                        "status": "OK",
                        "constrained": True,
                    },
                    {
                        "item_key": "octave",
                        "label": "倍频程",
                        "deviation_value": -4.0,
                        "deviation_display": "-4 dB",
                        "channel_index": 0,
                        "channel_label": "CH1",
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
                        "status": "OK",
                        "constrained": False,
                    },
                ],
            ),
        )
    )

    assert module.lb_overall.text() == "无结果"
    assert _table_value(module, 0, 0) == "声压级"
    assert _table_value(module, 0, 1) == "-1 dB"
    assert _table_value(module, 0, 2) == "通道1"
    assert _table_value(module, 0, 3) == "OK"
    assert _table_value(module, 1, 0) == "平均声压级"
    assert _table_value(module, 1, 2) == "通道1"
    assert _table_value(module, 1, 3) == "OK"
    assert _table_value(module, 2, 0) == "响度"
    assert _table_value(module, 2, 2) == "通道2"
    assert _table_value(module, 2, 3) == "无结果"
    assert _table_value(module, 3, 0) == "平均响度"
    assert _table_value(module, 3, 2) == "通道2"
    assert _table_value(module, 3, 3) == "无结果"
    assert _table_value(module, 4, 0) == "FFT频谱"
    assert _table_value(module, 4, 2) == "通道1"
    assert _table_value(module, 4, 3) == "OK"
    assert _table_value(module, 5, 0) == "倍频程"
    assert _table_value(module, 6, 0) == "AI分析"
    assert _table_value(module, 6, 3) == "OK"
    assert getattr(module, "error_label").isHidden() is False
    assert getattr(module, "error_label").text() == "MoSQITo unavailable"


def test_factory_reset_still_clears_to_the_empty_badge_state(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=_summary_payload(overall_status="pass"))
    )

    assert module.lb_overall.text() == "PASS"

    module.services.bus.factory_display_reset.emit("next_run_accepted")
    module.services.bus.factory_review_changed.emit(FactoryReviewPayload())
    module.services.bus.factory_state_changed.emit(_state("armed"))

    _assert_placeholder(module, badge_text="-")


def test_plain_result_changed_does_not_restore_result_after_factory_reset(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=_summary_payload(overall_status="pass"))
    )

    module.services.bus.factory_display_reset.emit("next_run_accepted")
    module.services.bus.factory_review_changed.emit(FactoryReviewPayload())
    module.services.bus.factory_state_changed.emit(_state("armed"))
    _assert_placeholder(module, badge_text="-")

    module.services.bus.result_changed.emit({"overall": "不合格"})

    _assert_placeholder(module, badge_text="-")
