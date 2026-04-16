from __future__ import annotations

import app.services_container as services_mod
from app.services_container import EventBus

from ._support import (
    _SelectionSettingsStub,
    _emit_owned_runtime_start,
    _enter_reviewing_state,
    _load_factory_mode_controller_module,
    _make_abort_payload,
    _make_config,
    _make_dynamic_services,
    _make_services,
)


def _make_stopped_payload(
    *,
    config_id: int = 101,
    run_id: str | None = None,
    trigger_source: str = "manual",
):
    stopped_payload_cls = getattr(services_mod, "FactoryRuntimeStoppedPayload", None)

    assert stopped_payload_cls is not None, "FactoryRuntimeStoppedPayload is missing"

    return stopped_payload_cls(
        config_id=config_id,
        run_id=run_id or f"run-{config_id}-1",
        trigger_source=trigger_source,
    )


def test_factory_mode_controller_starts_not_ready():
    controller_mod = _load_factory_mode_controller_module()
    state_cls = getattr(services_mod, "FactoryControllerState", None)

    assert state_cls is not None, "FactoryControllerState is missing"

    controller = controller_mod.FactoryModeController(EventBus())

    assert isinstance(controller.state, state_cls)
    assert controller.state.selected_config_ids == ()
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "not_ready"
    assert controller.state.trigger_source is None
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "请至少启用一个配置"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "请至少启用一个配置"
    assert controller.state.is_ready is False


def test_one_enabled_config_is_selection_ready_and_becomes_manual_start_target():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(202),))
    controller = controller_mod.FactoryModeController(bus, services)

    controller.set_selection((202,))

    assert controller.state.selected_config_ids == (202,)
    assert controller.state.run_phase == "armed"
    assert controller.state.selection_ready is True
    assert controller.state.selection_ready_reason is None
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None


def test_zero_enabled_configs_block_with_explicit_reason():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection(())

    assert controller.state.selected_config_ids == ()
    assert controller.state.run_phase == "not_ready"
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "请至少启用一个配置"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "请至少启用一个配置"


def test_runtime_validation_uses_first_error_from_highest_priority_enabled_config():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (_make_config(101), _make_config(202), _make_config(303)),
        validation_errors={
            202: ["priority-202-error", "priority-202-second-error"],
            101: ["priority-101-error"],
        },
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((202, 101, 303))

    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "priority-202-error"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "priority-202-error"


def test_initial_load_failure_blocks_with_config_load_failed_reason():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    services.config_service.load_config.side_effect = lambda config_id: (
        (_make_config(config_id))
        if config_id == 101
        else (_ for _ in ()).throw(RuntimeError("boom"))
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((202, 101))

    assert controller.state.selected_config_ids == (202, 101)
    assert controller.state.run_phase == "not_ready"
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置加载失败"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置加载失败"


def test_factory_mode_controller_owns_pair_selection_state():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)

    controller.set_selection((101, 202))

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.run_phase == "armed"
    assert controller.state.selection_ready is True
    assert controller.state.selection_ready_reason is None
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.state.is_ready is True
    assert states[-1] == controller.state


def test_set_selection_clears_review_payload_and_emits_cleared_review_contract():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    reset_reasons: list[str] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_display_reset.connect(reset_reasons.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus)

    reviews.clear()
    reset_reasons.clear()

    controller.set_selection((303, 404))

    assert controller.review_payload == review_payload_cls()
    assert controller.state.selected_config_ids == (303, 404)
    assert controller.state.displayed_config_id is None
    assert controller.state.trigger_source is None
    assert reviews == [review_payload_cls()]
    assert reset_reasons == ["selection_changed"]


def test_set_selection_preserves_review_context_for_noop_resubmit():
    controller_mod = _load_factory_mode_controller_module()

    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202))
    original_review_payload = _enter_reviewing_state(
        controller,
        bus,
        config_id=202,
        trigger_source="plc",
    )
    original_state = controller.state

    reset_reasons.clear()
    reviews.clear()
    states.clear()

    next_state = controller.set_selection((101, 202))

    assert next_state == original_state
    assert controller.state == original_state
    assert controller.review_payload == original_review_payload
    assert controller.state.run_phase == "reviewing"
    assert controller.state.displayed_config_id == 202
    assert controller.state.trigger_source == "plc"
    assert reset_reasons == []
    assert reviews == []
    assert states == []


def test_set_selection_from_stopped_clears_preserved_display_context_only_for_real_tuple_change():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((
        _make_config(101),
        _make_config(202),
        _make_config(303),
    ))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id="run-1",
        trigger_source="manual",
    )
    controller.handle_runtime_stopped(
        _make_stopped_payload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    reset_reasons.clear()
    reviews.clear()

    controller.set_selection((202, 303))

    assert controller.state.displayed_config_id is None
    assert controller.state.trigger_source is None
    assert controller.state.run_phase == "armed"
    assert reset_reasons == ["selection_changed"]
    assert reviews == [services_mod.FactoryReviewPayload()]


def test_set_selection_from_stopped_reorder_clears_preserved_display_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id="run-1",
        trigger_source="manual",
    )
    controller.handle_runtime_stopped(
        _make_stopped_payload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    reset_reasons.clear()
    reviews.clear()

    controller.set_selection((202, 101))

    assert controller.state.displayed_config_id is None
    assert controller.state.trigger_source is None
    assert controller.state.run_phase == "armed"
    assert reset_reasons == ["selection_changed"]
    assert reviews == [services_mod.FactoryReviewPayload()]


def test_set_selection_noop_refresh_preserves_stopped_display_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id="run-1",
        trigger_source="manual",
    )
    controller.handle_runtime_stopped(
        _make_stopped_payload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    original_state = controller.state
    reset_reasons.clear()
    reviews.clear()
    states.clear()

    next_state = controller.set_selection((101, 202))

    assert next_state == original_state
    assert controller.state == original_state
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "stopped"
    assert reset_reasons == []
    assert reviews == []
    assert states == []


def test_refresh_selection_same_tuple_preserves_stopped_display_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id="run-1",
        trigger_source="manual",
    )
    controller.handle_runtime_stopped(
        _make_stopped_payload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    reset_reasons.clear()
    reviews.clear()

    next_state = controller.refresh_selection()

    assert next_state == controller.state
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "stopped"
    assert reset_reasons == []
    assert reviews == []


def test_set_selection_from_review_to_zero_enabled_configs_goes_directly_to_not_ready():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

    reset_reasons.clear()
    reviews.clear()
    states.clear()

    controller.set_selection(())

    assert controller.review_payload == review_payload_cls()
    assert controller.state.selected_config_ids == ()
    assert controller.state.displayed_config_id is None
    assert controller.state.trigger_source is None
    assert controller.state.run_phase == "not_ready"
    assert controller.state.is_ready is False
    assert controller.state.selection_ready_reason == "请至少启用一个配置"
    assert controller.state.start_ready_reason == "请至少启用一个配置"
    assert reset_reasons == ["selection_changed"]
    assert reviews == [review_payload_cls()]
    assert states == [controller.state]
    assert states[0].run_phase == "not_ready"


def test_factory_mode_controller_allows_ordered_selection_without_manual_primary():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202))

    assert controller.state.run_phase == "armed"
    assert controller.state.is_ready is True
    assert controller.can_arm() is True


def test_factory_mode_controller_arms_only_after_runtime_validation_succeeds():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (_make_config(101), _make_config(202)),
        validation_errors={202: ["bad runtime config"]},
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202))

    assert controller.state.run_phase == "not_ready"
    assert controller.state.is_ready is False

    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202))

    assert controller.state.run_phase == "armed"
    assert controller.state.is_ready is True
    assert controller.can_arm() is True


def test_factory_mode_controller_rejects_start_when_backend_session_is_already_running():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.is_running.return_value = True
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202))

    assert controller.state.run_phase == "armed"
    assert controller.state.is_ready is True
    assert controller.can_arm() is False
    assert controller.start_manual({"sn": "SN-1"}) is False
    services.test.is_running.assert_called()
    services.test.start.assert_not_called()


def test_restore_selection_with_missing_configs_blocks_without_persisting_until_user_edit():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.restore_selection((101, 303), had_missing_configs=True)

    assert controller.state.selected_config_ids == (101, 303)
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置已失效"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置已失效"
    services.factory_selection_settings.save_selected_config_ids.assert_not_called()


def test_restore_selection_with_missing_configs_stays_blocking_until_later_user_edit():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.restore_selection((101, 303), had_missing_configs=True)

    controller.set_selection((101, 303))

    assert controller.state.selected_config_ids == (101, 303)
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置已失效"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置已失效"

    controller.set_selection((303, 101))

    assert controller.state.selected_config_ids == (303, 101)
    assert controller.state.selection_ready is True
    assert controller.state.selection_ready_reason is None
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None


def test_config_changed_prunes_deleted_selected_config_persists_and_blocks_until_user_edit():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.restore_selection((101, 202, 303), had_missing_configs=False)
    services.factory_selection_settings.save_selected_config_ids.reset_mock()

    assert services.config_service.delete_config(202) is True
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 303)
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置已失效"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置已失效"
    services.factory_selection_settings.save_selected_config_ids.assert_called_once_with(
        (101, 303)
    )


def test_runtime_prune_invalid_selection_stays_blocking_until_later_user_edit():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.restore_selection((101, 202, 303), had_missing_configs=False)

    assert services.config_service.delete_config(202) is True
    bus.config_changed.emit()
    assert controller.state.start_ready_reason == "配置已失效"

    controller.set_selection((303, 101))

    assert controller.state.selected_config_ids == (303, 101)
    assert controller.state.selection_ready is True
    assert controller.state.selection_ready_reason is None
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None


def test_busy_config_changed_prune_persists_now_but_defers_invalid_reason_until_idle():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.restore_selection((101, 202, 303), had_missing_configs=False)
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )
    services.factory_selection_settings.save_selected_config_ids.reset_mock()

    assert services.config_service.delete_config(202) is True
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 303)
    assert controller.state.run_phase == "running"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "当前测试运行中"
    services.factory_selection_settings.save_selected_config_ids.assert_called_once_with(
        (101, 303)
    )

    bus.factory_runtime_aborted.emit(
        _make_abort_payload(
            config_id=101,
            trigger_source="manual",
        )
    )

    assert controller.state.run_phase == "not_ready"
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置已失效"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置已失效"


def test_runtime_prune_save_failure_keeps_live_selection_preserves_restart_selection_and_emits_notice():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    notices: list[str] = []
    bus.factory_selection_notice.connect(notices.append)
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    services.factory_selection_settings = _SelectionSettingsStub(
        saved_selected_ids=(101, 202, 303),
        save_results=["配置保存失败"],
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.restore_selection((101, 202, 303), had_missing_configs=False)

    assert services.config_service.delete_config(202) is True
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 303)
    assert (
        services.factory_selection_settings.load_selected_config_ids()
        == (101, 202, 303)
    )
    assert notices == ["配置保存失败"]


def test_successful_later_controller_persist_clears_runtime_prune_notice():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    notices: list[str] = []
    bus.factory_selection_notice.connect(notices.append)
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )
    services.factory_selection_settings = _SelectionSettingsStub(
        saved_selected_ids=(101, 202, 303),
        save_results=["配置保存失败", None],
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.restore_selection((101, 202, 303), had_missing_configs=False)

    assert services.config_service.delete_config(202) is True
    bus.config_changed.emit()
    assert services.config_service.delete_config(303) is True
    bus.config_changed.emit()

    assert notices == ["配置保存失败", ""]
    assert services.factory_selection_settings.load_selected_config_ids() == (101,)


def test_config_changed_clears_config_load_failed_when_loading_recovers():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    broken = {"enabled": True}
    services = _make_dynamic_services(config_map)

    def _load_config(config_id: int):
        if config_id == 202 and broken["enabled"]:
            raise RuntimeError("boom")
        return dict(config_map[config_id])

    services.config_service.load_config.side_effect = _load_config
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((202, 101))

    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置加载失败"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置加载失败"

    broken["enabled"] = False
    bus.config_changed.emit()

    assert controller.state.selection_ready is True
    assert controller.state.selection_ready_reason is None
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
