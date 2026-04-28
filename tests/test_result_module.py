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
    assert module.table.rowCount() == 5
    assert _table_value(module, 0, 0) == "声压级"
    assert _table_value(module, 1, 0) == "响度"
    assert _table_value(module, 2, 0) == "FFT频谱"
    assert _table_value(module, 3, 0) == "倍频程"
    assert _table_value(module, 4, 0) == "AI分析"
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
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_state_changed.emit(
        _state("running", displayed_config_id=202, active_config_id=202)
    )
    assert module.lb_spl_metric_value.text() == "--"

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
    assert module.lb_spl_metric_value.text() == "61.0 dB"

    module.services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(config_id=101, run_id="run-2", trigger_source="manual")
    )
    assert module.lb_spl_metric_value.text() == "--"

    module.services.bus.spl_updated.emit(101, "run-1", 0, SimpleNamespace(spl_db=70.0))
    assert module.lb_spl_metric_value.text() == "--"
    module.services.bus.spl_updated.emit(101, "run-2", 0, SimpleNamespace(spl_db=62.0))
    assert module.lb_spl_metric_value.text() == "62.0 dB"


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
    assert module.lb_spl_metric_value.text() == "61.0 dB"
    assert module.lb_loudness_metric_value.text() == "1.25 sone"

    module.services.bus.factory_display_reset.emit("next_run_accepted")
    assert module.lb_spl_metric_value.text() == "--"
    assert module.lb_loudness_metric_value.text() == "--"

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
    assert module.table.rowCount() == 5
    assert _table_value(module, 0, 0) == "声压级"
    assert _table_value(module, 0, 1) == "6 dB"
    assert _table_value(module, 0, 2) == "通道2"
    assert _table_value(module, 0, 3) == "NG"
    assert _table_value(module, 1, 0) == "响度"
    assert _table_value(module, 1, 1) == "4 sone"
    assert _table_value(module, 1, 2) == "通道2"
    assert _table_value(module, 1, 3) == "NG"
    assert _table_value(module, 2, 0) == "FFT频谱"
    assert _table_value(module, 2, 2) == "通道1"
    assert _table_value(module, 2, 3) == "OK"
    assert _table_value(module, 3, 0) == "倍频程"
    assert _table_value(module, 3, 2) == "通道1"
    assert _table_value(module, 3, 3) == "OK"
    assert _table_value(module, 4, 0) == "AI分析"
    assert _table_value(module, 4, 1) == "--"
    assert _table_value(module, 4, 2) == "--"
    assert _table_value(module, 4, 3) == "OK"


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
    payload["rows"][3]["label"] = "Octave"

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(summary_payload=payload)
    )

    assert _table_value(module, 3, 0) == "倍频程"


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
    assert _table_value(module, 4, 0) == "AI分析"


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
    assert _table_value(module, 1, 0) == "响度"
    assert _table_value(module, 1, 2) == "通道2"
    assert _table_value(module, 1, 3) == "无结果"
    assert _table_value(module, 2, 0) == "FFT频谱"
    assert _table_value(module, 2, 2) == "通道1"
    assert _table_value(module, 4, 0) == "AI分析"
    assert _table_value(module, 4, 3) == "OK"
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
