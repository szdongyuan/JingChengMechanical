from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.services_container import FactoryReviewPayload, FactoryReviewWindow
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
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_module() -> ResultModule:
    services = SimpleNamespace(bus=_BusStub())
    return ResultModule(services)


def _table_value(module: ResultModule, row: int) -> str:
    item = module.table.item(row, 1)
    assert item is not None
    return item.text()


def _success_payload(*, overall: str = "合格") -> dict:
    return {
        "overall": overall,
        "ai_label": "Normal",
        "ai_score": 0.93,
        "ai_summary": "stable waveform",
        "ai_status": "success",
        "toolchain_name": "Toolchain",
        "toolchain_version": "1.0",
        "error_summary": None,
        "artifacts_json": ["artifact.json"],
        "spl_db": 82.4,
    }


def _make_review_payload(
    *,
    no_result: bool = False,
    error_message: str | None = None,
    judgment_payload: dict | None = None,
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
    )


def _assert_cleared(module: ResultModule) -> None:
    assert module.lb_overall.text() == "-"
    assert module.lb_overall.property("resultLevel") == "unknown"
    assert _table_value(module, 4) == "-"
    assert _table_value(module, 5) == "-"
    assert module.empty_state.isHidden() is False
    assert module.lb_empty_text.text() == "No data"


def _assert_no_result(module: ResultModule, *, error_message: str) -> None:
    assert module.lb_overall.text() == "-"
    assert module.lb_overall.property("resultLevel") == "unknown"
    assert _table_value(module, 4) == "无结果"
    assert _table_value(module, 5) == error_message
    assert module.empty_state.isHidden() is False


def test_next_run_reset_clears_result_rendered_from_factory_review_payload(qapp):
    module = _make_module()

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=_success_payload())
    )

    assert module.lb_overall.text() == "PASS"
    assert module.lb_overall.property("resultLevel") == "pass"
    assert _table_value(module, 4) == "分析完成"
    assert module.empty_state.isHidden() is True

    module.services.bus.factory_display_reset.emit("next_run_accepted")

    _assert_cleared(module)


def test_plain_result_changed_does_not_restore_result_after_factory_reset(qapp):
    module = _make_module()

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=_success_payload())
    )
    assert module.lb_overall.text() == "PASS"

    module.services.bus.factory_display_reset.emit("next_run_accepted")
    _assert_cleared(module)

    module.services.bus.result_changed.emit(_success_payload(overall="不合格"))

    _assert_cleared(module)


def test_reanalysis_start_clears_visible_result_immediately(qapp):
    module = _make_module()

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=_success_payload())
    )
    assert module.lb_overall.text() == "PASS"

    module.services.bus.factory_review_changed.emit(_make_review_payload(judgment_payload=None))

    _assert_cleared(module)


def test_manual_reanalysis_failure_keeps_explicit_no_result_review_state(qapp):
    module = _make_module()

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=_success_payload())
    )
    module.services.bus.factory_review_changed.emit(_make_review_payload(judgment_payload=None))

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            no_result=True,
            error_message="manual reanalysis failed",
            judgment_payload=None,
        )
    )

    _assert_no_result(module, error_message="manual reanalysis failed")


def test_auto_analysis_required_window_failure_keeps_no_confident_result_and_error(qapp):
    module = _make_module()

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            no_result=True,
            error_message="analysis window not fully captured",
            judgment_payload=None,
        )
    )

    _assert_no_result(module, error_message="analysis window not fully captured")


def test_manual_reanalysis_success_shows_result_again_after_no_result_review(qapp):
    module = _make_module()

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(
            no_result=True,
            error_message="analysis window not fully captured",
            judgment_payload=None,
        )
    )
    _assert_no_result(module, error_message="analysis window not fully captured")

    module.services.bus.factory_review_changed.emit(
        _make_review_payload(judgment_payload=_success_payload(overall="不合格"))
    )

    assert module.lb_overall.text() == "FAIL"
    assert module.lb_overall.property("resultLevel") == "fail"
    assert _table_value(module, 4) == "分析完成"
    assert _table_value(module, 5) == "-"
    assert module.empty_state.isHidden() is True
