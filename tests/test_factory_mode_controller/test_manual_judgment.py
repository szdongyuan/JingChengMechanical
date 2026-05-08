from __future__ import annotations

from types import SimpleNamespace

from app.service.factory_mode_controller import FactoryModeController
from app.services_container import EventBus, FactoryControllerState, FactoryReviewPayload


class _ManualResult:
    def __init__(self, success, message="", audio_data_id="audio-1"):
        self.success = success
        self.message = message
        self.audio_data_id = audio_data_id


class _TestStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def is_running(self):
        return False

    def apply_manual_judgment(self, *, config_id, run_id, label):
        self.calls.append((config_id, run_id, label))
        return self.result


def _make_controller(result):
    bus = EventBus()
    services = SimpleNamespace(
        test=_TestStub(result),
        config_service=SimpleNamespace(
            load_config=lambda config_id: {
                "config_id": config_id,
                "selected_channels": [0],
            },
            validate_factory_runtime=lambda config: [],
        ),
    )
    controller = FactoryModeController(bus, services)
    controller.restore_selection((101,), had_missing_configs=False)
    return controller, services, bus


def _enter_review(controller):
    controller._state = FactoryControllerState(
        selected_config_ids=(101,),
        displayed_config_id=101,
        run_phase="reviewing",
        selection_ready=True,
        start_ready=True,
    )
    controller._review_payload = FactoryReviewPayload(
        displayed_config_id=101,
        run_id="run-101",
        trigger_source="manual",
    )


def test_manual_ok_persists_and_resets_review_context():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("OK")

    assert result.success is True
    assert services.test.calls == [(101, "run-101", "OK")]
    assert resets == ["manual_judgment_ok"]
    assert controller.state.run_phase == "armed"
    assert controller.state.displayed_config_id is None


def test_manual_ng_uses_ng_reset_reason():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("NG")

    assert result.success is True
    assert services.test.calls == [(101, "run-101", "NG")]
    assert resets == ["manual_judgment_ng"]


def test_manual_judgment_normalizes_label_before_backend_call():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment(" ok ")

    assert result.success is True
    assert services.test.calls == [(101, "run-101", "OK")]
    assert resets == ["manual_judgment_ok"]


def test_manual_judgment_rejects_invalid_label_without_reset():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("MAYBE")

    assert result.success is False
    assert "无效判定" in result.message
    assert services.test.calls == []
    assert resets == []


def test_manual_judgment_rejects_busy_state_without_reset():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    controller._state = FactoryControllerState(
        selected_config_ids=(101,),
        active_config_id=101,
        displayed_config_id=101,
        run_phase="running",
    )

    result = controller.apply_manual_judgment("OK")

    assert result.success is False
    assert "测试仍在进行中" in result.message
    assert services.test.calls == []
    assert resets == []


def test_manual_judgment_rejects_no_display_context():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)

    result = controller.apply_manual_judgment("OK")

    assert result.success is False
    assert "没有可判定的测试结果" in result.message
    assert services.test.calls == []
    assert resets == []


def test_manual_judgment_backend_failure_does_not_reset():
    controller, services, bus = _make_controller(
        _ManualResult(False, "当前测试音频记录不存在")
    )
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("OK")

    assert result.success is False
    assert result.message == "当前测试音频记录不存在"
    assert resets == []
    assert controller.state.run_phase == "reviewing"


def test_stopped_run_preserves_run_id_for_manual_judgment():
    from app.services_container import FactoryRuntimeStoppedPayload

    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    controller._state = FactoryControllerState(
        selected_config_ids=(101,),
        active_config_id=101,
        displayed_config_id=101,
        run_phase="running",
        trigger_source="manual",
    )
    controller._owned_runtime_config_id = 101
    controller._owned_runtime_run_id = "run-stop"

    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(
            config_id=101,
            run_id="run-stop",
            trigger_source="manual",
        )
    )

    result = controller.apply_manual_judgment("OK")

    assert result.success is True
    assert services.test.calls == [(101, "run-stop", "OK")]
    assert resets == ["manual_judgment_ok"]
