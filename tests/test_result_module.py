from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
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
                "deviation_value": -3.0,
                "deviation_display": "-3 dB",
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
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=101,
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
    assert _table_value(module, 0, 0) == "SPL"
    assert _table_value(module, 0, 1) == "-"
    assert _table_value(module, 0, 2) == "-"
    assert _table_value(module, 0, 3) == "-"
    assert getattr(module, "error_label").isHidden() is True


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
    assert _table_value(module, 0, 0) == "SPL"
    assert _table_value(module, 0, 1) == "6 dB"
    assert _table_value(module, 0, 2) == "CH2"
    assert _table_value(module, 0, 3) == "NG"
    assert _table_value(module, 3, 0) == "AI分析"
    assert _table_value(module, 3, 1) == "--"
    assert _table_value(module, 3, 2) == "--"
    assert _table_value(module, 3, 3) == "OK"


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


def test_result_module_shows_no_result_plus_error_text_from_summary_payload(qapp):
    module = _make_module()
    module.services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            no_result=True,
            error_message="AI timeout",
            judgment_payload={"overall": "合格"},
            summary_payload=_summary_payload(
                overall_status="no_result",
                error_message="AI timeout",
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
                        "deviation_value": -3.0,
                        "deviation_display": "-3 dB",
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
                        "status": "无结果",
                        "constrained": True,
                    },
                ],
            ),
        )
    )

    assert module.lb_overall.text() == "无结果"
    assert _table_value(module, 0, 0) == "SPL"
    assert _table_value(module, 0, 1) == "-1 dB"
    assert _table_value(module, 0, 2) == "CH1"
    assert _table_value(module, 0, 3) == "OK"
    assert _table_value(module, 3, 0) == "AI分析"
    assert _table_value(module, 3, 3) == "无结果"
    assert getattr(module, "error_label").isHidden() is False
    assert getattr(module, "error_label").text() == "AI timeout"


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
