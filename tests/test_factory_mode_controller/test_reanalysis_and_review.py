from __future__ import annotations

import pytest
import app.services_container as services_mod
from app.services_container import EventBus

from ._support import (
    _accept_start,
    _enter_reviewing_state,
    _emit_owned_runtime_start,
    _full_no_result_summary,
    _load_factory_mode_controller_module,
    _make_config,
    _make_real_manager_factory_context,
    _make_review_payload,
    _make_runtime_config_payload,
    _make_services,
    _run_id_for,
)


def test_factory_mode_controller_consumes_runtime_events_and_review_payload():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)
    runtime_phase_cls = getattr(services_mod, "FactoryRuntimeConfigPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"
    assert runtime_phase_cls is not None, "FactoryRuntimeConfigPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202))

    _enter_reviewing_state(controller, bus)

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.review_payload == _make_review_payload()
    assert reviews[-1] == _make_review_payload()


def test_factory_mode_controller_reanalysis_failure_returns_to_reviewing_without_result():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reanalyzing"
    assert controller.state.displayed_config_id == 101

    controller.finish_reanalysis_failure("manual reanalysis failed")

    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "manual reanalysis failed"
    assert controller.review_payload.summary_payload == _full_no_result_summary(
        "manual reanalysis failed"
    )
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None


def test_stop_triggered_auto_analysis_success_reaches_controller_reviewing_state(
    monkeypatch,
):
    controller, services, reset_reasons, reviews, _, _ = _make_real_manager_factory_context(
        monkeypatch
    )
    config_map = {
        101: {
            **_make_config(101),
            "allow_incomplete_recording_analysis": True,
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1000,
        },
        202: _make_config(202),
    }
    services.config_service.load_config.side_effect = lambda config_id: dict(
        config_map[config_id]
    )

    started = controller.start_manual({"sn": "SN-1"})

    assert started is True
    assert controller.state.run_phase == "running"
    reset_reasons.clear()
    reviews.clear()

    stopped = controller.stop_active_run()

    assert stopped is True
    assert services.test.is_running(config_id=101) is False
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "manual"
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.trigger_source == "manual"
    assert reset_reasons == []
    assert reviews[-1].displayed_config_id == 101


def test_begin_reanalysis_clears_summary_payload_and_chart_payload_without_resetting_review_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    run_id = _run_id_for(101)
    review_payload = _make_review_payload(
        displayed_config_id=101,
        run_id=run_id,
        trigger_source="manual",
        judgment_payload={"overall": "合格"},
        summary_payload={"overall_status": "pass", "overall_label": "PASS", "rows": []},
        chart_payload={0: {"loudness": {}, "spl": {}, "fft": {}, "octave": {}}},
    )

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id=run_id,
        trigger_source="manual",
    )
    bus.factory_runtime_analyzing_entered.emit(
        _make_runtime_config_payload(config_id=101, run_id=run_id)
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(review_payload)
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reanalyzing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.run_id == run_id
    assert controller.review_payload.default_window == review_payload.default_window
    assert controller.review_payload.fallback_window == review_payload.fallback_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is False
    assert controller.review_payload.error_message is None
    assert controller.review_payload.summary_payload is None
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert reviews == [controller.review_payload]
    assert [state.run_phase for state in states] == ["reanalyzing"]


def test_factory_mode_controller_preserves_committed_partial_no_result_reanalysis_payload():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)
    partial_summary_payload = {
        "overall_status": "no_result",
        "overall_label": "无结果",
        "error_message": "AI timeout",
        "rows": [
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
                "channel_index": 0,
                "channel_label": "CH1",
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
    }
    partial_chart_payload = {
        0: {
            "loudness": {"series": [2.0, 3.0], "unit": "sone"},
            "spl": {"series": [70.0, 71.0]},
            "fft": {"series": [55.0, 48.0]},
            "octave": {"series": [57.0, 50.0]},
        }
    }
    committed_payload = _make_review_payload(
        displayed_config_id=101,
        run_id=original_review.run_id,
        trigger_source="manual",
        no_result=True,
        error_message="AI timeout",
        judgment_payload={"overall": "合格", "analysis_window": (1.25, 3.25)},
        summary_payload=partial_summary_payload,
        chart_payload=partial_chart_payload,
    )

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)

    controller.handle_runtime_reanalysis_completed(committed_payload)

    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "AI timeout"
    assert controller.review_payload.summary_payload is partial_summary_payload
    assert controller.review_payload.chart_payload is partial_chart_payload
    assert controller.review_payload.judgment_payload == {
        "overall": "合格",
        "analysis_window": (1.25, 3.25),
    }
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


@pytest.mark.parametrize(
    ("payload_kwargs", "expected_error_message"),
    [
        (
            {"no_result": True, "judgment_payload": None},
            "manual reanalysis failed",
        ),
        (
            {"error_message": "AI timeout", "judgment_payload": None},
            "AI timeout",
        ),
    ],
)
def test_factory_mode_controller_reanalysis_completed_terminal_no_result_payload_restores_start_ready(
    payload_kwargs,
    expected_error_message,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((202, 101))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)

    controller.handle_runtime_reanalysis_completed(
        _make_review_payload(
            displayed_config_id=101,
            run_id=original_review.run_id,
            trigger_source="manual",
            summary_payload=None,
            **payload_kwargs,
        )
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == expected_error_message
    assert controller.review_payload.summary_payload == _full_no_result_summary(
        expected_error_message
    )
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_accepts_successful_reanalysis_payload_without_run_id():
    controller_mod = _load_factory_mode_controller_module()
    review_window_cls = getattr(services_mod, "FactoryReviewWindow", None)
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True

    controller.handle_runtime_reanalysis_completed(
        review_payload_cls(
            displayed_config_id=101,
            run_id=None,
            trigger_source="manual",
            captured_duration_sec=6.5,
            default_window=review_window_cls(start_sec=1.25, end_sec=3.25),
            fallback_window=None,
            manual_reanalysis_allowed=False,
            no_result=False,
            error_message=None,
            judgment_payload={"overall": "PASS", "source": "reanalysis"},
            summary_payload=None,
            chart_payload={"series": [1.0, 2.0, 3.0]},
        )
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.displayed_config_id == 101
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.captured_duration_sec == 6.5
    assert controller.review_payload.judgment_payload == {
        "overall": "PASS",
        "source": "reanalysis",
    }
    assert controller.review_payload.chart_payload == {"series": [1.0, 2.0, 3.0]}
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_accepts_reanalysis_payload_without_displayed_config_id():
    controller_mod = _load_factory_mode_controller_module()
    review_window_cls = getattr(services_mod, "FactoryReviewWindow", None)
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True

    controller.handle_runtime_reanalysis_completed(
        review_payload_cls(
            displayed_config_id=None,
            run_id=original_review.run_id,
            trigger_source="manual",
            captured_duration_sec=6.5,
            default_window=review_window_cls(start_sec=1.25, end_sec=3.25),
            fallback_window=None,
            manual_reanalysis_allowed=False,
            no_result=False,
            error_message=None,
            judgment_payload={"overall": "PASS", "source": "reanalysis"},
            summary_payload=None,
            chart_payload={"series": [1.0, 2.0, 3.0]},
        )
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.captured_duration_sec == 6.5
    assert controller.review_payload.judgment_payload == {
        "overall": "PASS",
        "source": "reanalysis",
    }
    assert controller.review_payload.chart_payload == {"series": [1.0, 2.0, 3.0]}
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_rejects_fully_sparse_reanalysis_payload():
    controller_mod = _load_factory_mode_controller_module()
    review_window_cls = getattr(services_mod, "FactoryReviewWindow", None)
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True

    pending_review = controller.review_payload
    pending_state = controller.state

    controller.handle_runtime_reanalysis_completed(
        review_payload_cls(
            displayed_config_id=None,
            run_id=None,
            trigger_source="manual",
            captured_duration_sec=6.5,
            default_window=review_window_cls(start_sec=1.25, end_sec=3.25),
            fallback_window=None,
            manual_reanalysis_allowed=False,
            no_result=False,
            error_message=None,
            judgment_payload={"overall": "PASS", "source": "reanalysis"},
            summary_payload=None,
            chart_payload={"series": [1.0, 2.0, 3.0]},
        )
    )

    assert controller.state == pending_state
    assert controller.review_payload == pending_review
    assert controller.state.run_phase == "reanalyzing"
    assert controller.review_payload.run_id is not None
    assert [state.run_phase for state in states] == ["reanalyzing"]
    assert reviews == [pending_review]


@pytest.mark.parametrize(
    ("displayed_config_id", "run_id"),
    [
        (None, _run_id_for(202)),
        (202, None),
    ],
)
def test_factory_mode_controller_rejects_mismatched_sparse_reanalysis_payloads(
    displayed_config_id,
    run_id,
):
    controller_mod = _load_factory_mode_controller_module()
    review_window_cls = getattr(services_mod, "FactoryReviewWindow", None)
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True

    pending_review = controller.review_payload
    pending_state = controller.state

    controller.handle_runtime_reanalysis_completed(
        review_payload_cls(
            displayed_config_id=displayed_config_id,
            run_id=run_id,
            trigger_source="manual",
            captured_duration_sec=6.5,
            default_window=review_window_cls(start_sec=1.25, end_sec=3.25),
            fallback_window=None,
            manual_reanalysis_allowed=False,
            no_result=False,
            error_message=None,
            judgment_payload={"overall": "PASS", "source": "wrong"},
            summary_payload=None,
            chart_payload={"series": [9.0]},
        )
    )

    assert controller.state == pending_state
    assert controller.review_payload == pending_review
    assert controller.review_payload.run_id == original_review.run_id
    assert [state.run_phase for state in states] == ["reanalyzing"]
    assert reviews == [pending_review]


def test_factory_mode_controller_reanalysis_missing_session_unwinds_to_reviewing_no_result():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()
    summary_payload = _full_no_result_summary("no session for config 101")

    def _rejudge(config_id, start_sec, end_sec):
        error_message = f"no session for config {config_id}"
        bus.factory_runtime_reanalysis_completed.emit(
            review_payload_cls(
                displayed_config_id=config_id,
                run_id=None,
                trigger_source="manual",
                captured_duration_sec=None,
                default_window=None,
                fallback_window=None,
                manual_reanalysis_allowed=False,
                no_result=True,
                error_message=error_message,
                judgment_payload=None,
                summary_payload=summary_payload,
            )
        )

    services.test.rejudge.side_effect = _rejudge

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "no session for config 101"
    assert controller.review_payload.summary_payload is summary_payload
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_reanalysis_empty_exception_message_unwinds_to_reviewing_no_result():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.rejudge.side_effect = RuntimeError("")
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review = _enter_reviewing_state(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    reviews.clear()
    states.clear()

    try:
        started = controller.begin_reanalysis(1.25, 3.25)
    except RuntimeError as exc:
        started = exc

    assert started is False
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "分析结果不可用"
    assert controller.review_payload.summary_payload == _full_no_result_summary("")
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_ignores_mismatched_factory_runtime_review_payload():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    controller.start_manual()
    reviews.clear()

    controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(displayed_config_id=202, trigger_source="plc")
    )

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "analyzing"
    assert controller.state.trigger_source == "manual"
    assert reviews == []


@pytest.mark.parametrize(
    ("signal_name", "review_kwargs"),
    [
        ("factory_runtime_auto_analysis_succeeded", {}),
        (
            "factory_runtime_auto_analysis_no_result",
            {"no_result": True, "judgment_payload": None},
        ),
        (
            "factory_runtime_auto_analysis_error",
            {
                "no_result": True,
                "error_message": "analysis window missing",
                "judgment_payload": None,
            },
        ),
    ],
)
def test_factory_mode_controller_requires_analyzing_before_reviewing_for_auto_completion(
    signal_name,
    review_kwargs,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    run_id = _run_id_for(101)

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id=run_id,
        trigger_source="manual",
    )
    reviews.clear()

    getattr(bus, signal_name).emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=run_id,
            trigger_source="manual",
            **review_kwargs,
        )
    )

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert reviews == []
