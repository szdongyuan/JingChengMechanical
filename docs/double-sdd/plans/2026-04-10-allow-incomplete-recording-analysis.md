# Allow Incomplete Recording Analysis Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `允许分析不完整录音` config option and change manual stop so it either preserves the stopped display in a new `stopped` phase or reuses the normal auto-analysis flow when the default analysis window is already fully captured.

**Architecture:** The implementation is split into five slices. First, add the config field end-to-end through model, migration, service, and config UI. Then add the new runtime contracts and `stopped` controller phase, teach `TestManager` to branch stop outcomes based on capture eligibility and analysis races, and finally update the UI modules so stopped runs preserve the right display state without unlocking review-only controls unless a real review payload exists.

**Tech Stack:** Python, PyQt6, Peewee, pytest, numpy

---

## File Structure / Responsibilities

- `model/test_config_table.py`
  Add the persisted boolean config field for incomplete-recording stop analysis.
- `app/service/db_connection_service.py`
  Migrate legacy databases to include the new config column.
- `app/service/config_service.py`
  Load, save, default, and validate the new config field.
- `ui/modules/config.py`
  Render the checkbox and wire it through `_populate_detail()` / `_collect_data()`.
- `app/services_container.py`
  Extend factory-mode dataclasses/signals with the new stopped runtime payload and stopped phase readiness semantics.
- `app/service/factory_mode_controller.py`
  Consume the new stopped signal, preserve stopped display context, and clear it only on a true selection change / new accepted run / abort.
- `app/service/analysis/test_manager.py`
  Replace the current stop-as-cleanup path with phase-aware stop branching: stopped-without-analysis, stop-triggered analysis, and analyzing-stop race suppression.
- `ui/modules/workorder.py`
  Render `当前阶段：已停止`.
- `ui/modules/control.py`
  Keep start enabled in `stopped` while still disabling it during active/busy phases.
- `ui/modules/result.py`
  Preserve the visible in-progress/result state on a normal stop that does not emit a display reset.
- `ui/modules/stft.py`
  Keep rendering the stopped run while hiding drag/analyze controls unless the controller is actually in `reviewing`/`reanalyzing`.
- `ui/modules/spectrum.py`
  Keep committed chart content visible in `stopped` when a review payload already exists.
- `ui/modules/spl.py`
  Keep committed chart content visible in `stopped` when a review payload already exists.
- `ui/modules/loudness.py`
  Keep committed chart content visible in `stopped` when a review payload already exists.
- `ui/modules/octave.py`
  Keep committed chart content visible in `stopped` when a review payload already exists.
- `app/main_window.py`
  Keep the analysis toolbar available for stopped display context and preserve stopped-channel resolution rules.

## Task 1: Persist and Validate `allow_incomplete_recording_analysis`

**Files:**
- Modify: `model/test_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/config_service.py`
- Test: `tests/test_test_config_table.py`
- Test: `tests/test_db_connection_service.py`
- Test: `tests/test_config_service.py`

- [ ] **Step 1: Write the failing model/migration/service tests**

```python
def test_model_defines_allow_incomplete_recording_analysis_with_false_default():
    assert "allow_incomplete_recording_analysis" in TestConfigTable._meta.fields
    config = TestConfigTable.create(config_name="Incomplete Stop Defaults")
    assert config.allow_incomplete_recording_analysis is False


def test_connect_migrates_allow_incomplete_recording_analysis_column(monkeypatch):
    svc.connect()
    columns = _table_columns(tmp_db, "test_config_table")
    assert "allow_incomplete_recording_analysis" in columns
    assert columns["allow_incomplete_recording_analysis"][4] == "0"


def test_load_config_defaults_allow_incomplete_recording_analysis_to_false_for_legacy_rows(monkeypatch):
    svc.connect()
    loaded = ConfigService(bus=None, golden_sample_service=SimpleNamespace()).load_config(1)
    assert loaded["allow_incomplete_recording_analysis"] is False


def test_save_and_load_config_round_trips_allow_incomplete_recording_analysis():
    payload = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["allow_incomplete_recording_analysis"] = True
    svc.save_config(payload)
    loaded = svc.load_config(config_id)
    assert loaded["allow_incomplete_recording_analysis"] is True


def test_validate_rejects_non_boolean_allow_incomplete_recording_analysis():
    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "allow_incomplete_recording_analysis": "yes",
            "scan_triggers_test": False,
            "selected_channels": [],
            "channels": [],
        }
    )
    assert any("允许分析不完整录音" in err for err in errors)


def test_allow_incomplete_recording_analysis_does_not_relax_window_validity():
    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 2500,
            "allow_incomplete_recording_analysis": True,
            "scan_triggers_test": False,
            "selected_channels": [],
            "channels": [],
        }
    )
    assert any("分析窗起始偏移加分析时长不能超过录音时长" in err for err in errors)


def test_validate_factory_runtime_still_rejects_overlong_window_when_flag_enabled():
    errors = svc.validate_factory_runtime(
        {
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 2500,
            "allow_incomplete_recording_analysis": True,
        }
    )
    assert any("分析窗起始偏移加分析时长不能超过录音时长" in err for err in errors)
```

- [ ] **Step 2: Run the targeted tests to confirm the field is missing**

Run: `pytest tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_config_service.py -k "allow_incomplete_recording_analysis or trigger_cleanup_fields_with_defaults" -v`
Expected: FAIL with missing field / missing load-save key / missing validation coverage.

- [ ] **Step 3: Implement the minimal persistence and validation changes**

```python
class TestConfigTable(BaseModel):
    allow_incomplete_recording_analysis = BooleanField(default=False)


result = {
    "allow_incomplete_recording_analysis": bool(
        getattr(cfg, "allow_incomplete_recording_analysis", False)
    ),
}

TestConfigTable.update(
    allow_incomplete_recording_analysis=bool(
        data.get("allow_incomplete_recording_analysis", False)
    ),
)

if not isinstance(data.get("allow_incomplete_recording_analysis", False), bool):
    errors.append("允许分析不完整录音必须为布尔值")
```

- [ ] **Step 4: Run the focused persistence suite**

Run: `pytest tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_config_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add model/test_config_table.py app/service/db_connection_service.py app/service/config_service.py tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_config_service.py
git commit -m "feat(config): persist incomplete recording analysis flag"
```

### Task 2: Wire the Config Checkbox Through the Config Module

**Files:**
- Modify: `ui/modules/config.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write the failing config-module tests**

```python
def test_populate_detail_loads_allow_incomplete_recording_analysis_checkbox(qapp):
    module._populate_detail(
        {
            "config_id": 101,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "allow_incomplete_recording_analysis": True,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [],
        }
    )
    assert module.chk_allow_incomplete_recording_analysis.isChecked() is True


def test_collect_data_includes_allow_incomplete_recording_analysis(qapp):
    module.chk_allow_incomplete_recording_analysis.setChecked(True)
    data = module._collect_data()
    assert data["allow_incomplete_recording_analysis"] is True


def test_checkbox_defaults_to_unchecked_for_newly_loaded_config(qapp):
    module._populate_detail(
        {
            "config_id": 101,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "allow_incomplete_recording_analysis": False,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [],
        }
    )
    assert module.chk_allow_incomplete_recording_analysis.isChecked() is False


def test_checkbox_uses_approved_visible_label(qapp):
    assert module.chk_allow_incomplete_recording_analysis.text() == "允许分析不完整录音"


def test_allow_incomplete_recording_analysis_checkbox_is_positioned_with_analysis_window_controls(qapp):
    checkbox_row, _ = module.global_form_layout.getWidgetPosition(
        module.chk_allow_incomplete_recording_analysis
    )
    analysis_row, _ = module.global_form_layout.getWidgetPosition(module.spin_analysis)
    offset_row, _ = module.global_form_layout.getWidgetPosition(
        module.spin_analysis_window_offset
    )

    assert analysis_row < checkbox_row
    assert checkbox_row == offset_row + 1


def test_on_save_persists_allow_incomplete_recording_analysis_without_touching_ai_binding(qapp):
    module.chk_allow_incomplete_recording_analysis.setChecked(True)
    module._on_save()
    assert config_service.saved_configs[0]["allow_incomplete_recording_analysis"] is True
```

- [ ] **Step 2: Run the config-module slice**

Run: `pytest tests/test_config_module.py -k "allow_incomplete_recording_analysis" -v`
Expected: FAIL with missing widget / missing collected key / missing save payload field.

- [ ] **Step 3: Implement the checkbox and data wiring**

```python
self.global_form_layout = gf_lay

self.chk_allow_incomplete_recording_analysis = QCheckBox("允许分析不完整录音")
gf_lay.addRow(self.chk_allow_incomplete_recording_analysis)

self.chk_allow_incomplete_recording_analysis.setChecked(
    bool(data.get("allow_incomplete_recording_analysis", False))
)

"allow_incomplete_recording_analysis": (
    self.chk_allow_incomplete_recording_analysis.isChecked()
),
```

- [ ] **Step 4: Run the focused UI/config suite**

Run: `pytest tests/test_config_module.py tests/test_config_service.py -k "allow_incomplete_recording_analysis" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/modules/config.py tests/test_config_module.py tests/test_config_service.py
git commit -m "feat(config-ui): expose incomplete recording analysis toggle"
```

### Task 3: Add the `stopped` Factory Runtime Contract and Controller Phase

**Files:**
- Modify: `app/services_container.py`
- Modify: `app/service/factory_mode_controller.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `ui/modules/workorder.py`
- Modify: `ui/modules/control.py`
- Test: `tests/test_service_container.py`
- Test: `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- Test: `tests/test_factory_mode_controller/test_cleanup_and_abort.py`
- Test: `tests/test_factory_mode_controller/test_readiness_and_selection.py`
- Test: `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- Test: `tests/test_factory_mode_controller/test_trigger_routing.py`
- Test: `tests/test_test_manager/test_stop_behavior.py`
- Test: `tests/test_workorder_module.py`
- Test: `tests/test_control_module.py`

- [ ] **Step 1: Write the failing contract/controller/UI state tests**

```python
def test_factory_runtime_stopped_payload_contract_is_explicit():
    assert [field.name for field in fields(FactoryRuntimeStoppedPayload)] == [
        "config_id",
        "run_id",
        "trigger_source",
    ]


def test_factory_mode_controller_consumes_runtime_stopped_without_display_reset():
    prior_review = _make_review_payload(displayed_config_id=101, run_id="older-run", trigger_source="manual")
    controller._review_payload = prior_review
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    assert controller.state.run_phase == "stopped"
    assert reset_reasons == []
    assert controller.review_payload == prior_review
    assert reviews == []
    assert controller.state.trigger_source == "manual"
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None


def test_factory_mode_controller_consumes_factory_runtime_stopped_bus_signal():
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    bus.factory_runtime_stopped.emit(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    assert controller.state.run_phase == "stopped"


def test_factory_mode_controller_stop_active_run_waits_for_backend_signal_before_clearing_state():
    controller.start_manual()

    stopped = controller.stop_active_run()

    assert stopped is True
    assert controller.state.run_phase == "running"
    assert controller.state.displayed_config_id == 101
    assert reset_reasons == []


def test_factory_mode_controller_stop_active_run_returns_false_without_state_change_when_backend_declines_stop():
    controller.start_manual()
    services.test.stop_active_run.return_value = False
    prior_state = controller.state
    prior_review = controller.review_payload

    stopped = controller.stop_active_run()

    assert stopped is False
    assert controller.state == prior_state
    assert controller.review_payload == prior_review
    assert reset_reasons == []


def test_factory_mode_controller_stop_active_run_returns_false_without_state_change_when_backend_stop_raises():
    controller.start_manual()
    services.test.stop_active_run.side_effect = RuntimeError("stop failed")
    prior_state = controller.state
    prior_review = controller.review_payload

    stopped = controller.stop_active_run()

    assert stopped is False
    assert controller.state == prior_state
    assert controller.review_payload == prior_review
    assert reset_reasons == []


def test_test_manager_stop_active_run_emits_factory_runtime_stopped_on_successful_factory_stop(monkeypatch):
    bus, _, mgr, session, recorder, monitor, partial_buf = _make_running_factory_manager(monkeypatch)

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is True
    payload = bus.factory_runtime_stopped.emit.call_args.args[0]
    assert payload.config_id == 1
    assert payload.run_id == session._workorder["factory_run_id"]
    assert payload.trigger_source == "manual"
    assert mgr.is_running(config_id=1) is False
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_factory_mode_controller_direct_backend_stop_transitions_to_stopped_without_display_reset(
    monkeypatch,
):
    controller, services, reset_reasons, reviews, _, recorder_cls = (
        _make_controller_with_real_manager(monkeypatch)
    )

    stopped = controller.stop_active_run()

    assert stopped is True
    assert services.test.is_running(config_id=101) is False
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "stopped"
    assert controller.state.trigger_source == "manual"
    assert reset_reasons == []
    assert reviews == []


def test_workorder_phase_summary_renders_stopped_label(qapp):
    services.bus.factory_state_changed.emit(
        FactoryControllerState(selected_config_ids=(101,), run_phase="stopped")
    )
    assert module.lbl_phase_summary.text() == "当前阶段：已停止"


def test_control_start_button_remains_enabled_in_stopped_state(qapp):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="stopped",
            selection_ready=True,
            start_ready=True,
        )
    )
    assert module.btn_start.isEnabled() is True


def test_control_stop_button_is_disabled_in_stopped_state(qapp):
    module, _ = _make_module(initial_state=FactoryControllerState(selected_config_ids=(101,), run_phase="stopped"))
    assert module.btn_stop.isEnabled() is False


def test_workorder_keeps_config_selector_unlocked_in_stopped_state(qapp):
    module, services = _make_module(saved_selected_ids=(101,))
    services.bus.factory_state_changed.emit(
        FactoryControllerState(selected_config_ids=(101,), run_phase="stopped")
    )
    assert module.config_selector.is_locked() is False


def test_set_selection_from_stopped_clears_preserved_display_context_only_for_real_tuple_change():
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )

    controller.set_selection((202, 303))

    assert controller.state.displayed_config_id is None
    assert reset_reasons == ["selection_changed"]


def test_set_selection_from_stopped_reorder_clears_preserved_display_context():
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )

    controller.set_selection((202, 101))

    assert controller.state.displayed_config_id is None
    assert reset_reasons == ["selection_changed"]


def test_set_selection_noop_refresh_preserves_stopped_display_context():
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )

    next_state = controller.set_selection((101, 202))

    assert next_state == controller.state
    assert controller.state.displayed_config_id == 101
    assert reset_reasons == []


def test_refresh_selection_same_tuple_preserves_stopped_display_context():
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )

    next_state = controller.refresh_selection()

    assert next_state == controller.state
    assert controller.state.displayed_config_id == 101
    assert reset_reasons == []
    assert reviews == []


def test_factory_mode_controller_ignores_late_auto_analysis_completion_after_runtime_stopped():
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_analyzing(
        FactoryRuntimeConfigPayload(config_id=101, run_id="run-1")
    )
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )

    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(displayed_config_id=101, run_id="run-1", trigger_source="manual")
    )

    assert controller.state.run_phase == "stopped"
    assert controller.review_payload == FactoryReviewPayload()


def test_newly_accepted_run_clears_stopped_display_context_via_next_run_accepted():
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )

    _emit_owned_runtime_start(controller, bus, config_id=202, run_id="run-2", trigger_source="plc")

    assert controller.state.active_config_id == 202
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "running"
    assert reset_reasons == ["next_run_accepted"]


def test_start_manual_from_stopped_accepts_next_run_and_clears_preserved_context():
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-1", trigger_source="manual")
    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    _accept_start(services)

    started = controller.start_manual()

    assert started is True
    _emit_owned_runtime_start(controller, bus, config_id=101, run_id="run-2", trigger_source="manual")
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
```

- [ ] **Step 2: Run the stopped-phase slice**

Run: `pytest tests/test_service_container.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_cleanup_and_abort.py tests/test_factory_mode_controller/test_readiness_and_selection.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_test_manager/test_stop_behavior.py tests/test_workorder_module.py tests/test_control_module.py -v`
Expected: FAIL with missing payload/signal/phase label, missing backend `factory_runtime_stopped` producer, stop still resetting review/display context, or stopped-state selection/late-completion guards still missing.

- [ ] **Step 3: Implement the new runtime signal, phase, and controller semantics**

```python
@dataclass(frozen=True)
class FactoryRuntimeStoppedPayload:
    config_id: int
    run_id: str
    trigger_source: str


class EventBus(QObject):
    factory_runtime_stopped = pyqtSignal(object)


FactoryControllerState._START_READY_SELECTION_PHASES = frozenset({"idle", "armed", "reviewing", "stopped"})

_PHASE_LABELS["stopped"] = "已停止"

def handle_runtime_stopped(self, payload) -> None:
    if not self._owns_runtime_payload(
        config_id=payload.config_id,
        run_id=payload.run_id,
        allowed_phases=frozenset({"running", "analyzing"}),
    ):
        return
    self._owned_runtime_config_id = None
    self._owned_runtime_run_id = None
    self._state = self._state_with_readiness(replace(
        self._state,
        active_config_id=None,
        displayed_config_id=payload.config_id,
        run_phase="stopped",
        trigger_source=payload.trigger_source,
    ))
    self._emit_state()


def handle_runtime_review_ready(self, payload: FactoryReviewPayload) -> None:
    if not self._owns_active_review_payload(payload):
        return
    self._owned_runtime_config_id = None
    self._owned_runtime_run_id = None
    self._apply_review_payload(payload)


def handle_runtime_no_result(self, payload: FactoryReviewPayload) -> None:
    if not self._owns_active_review_payload(payload):
        return
    self._owned_runtime_config_id = None
    self._owned_runtime_run_id = None
    self._apply_review_payload(payload)


def stop_active_run(self, config_id: int | None = None) -> bool:
    stopped = self._stop_one(config_id, notify_factory_runtime_abort=False)
    if stopped and self._is_factory_runtime_session(config_id):
        self._emit_optional_signal(
            "factory_runtime_stopped",
            self._make_factory_runtime_stopped_payload(config_id),
        )
    return stopped
```

Keep the existing selection no-op short-circuit intact so same-tuple refreshes preserve stopped display context, but let real tuple changes clear the preserved stopped display through the normal `selection_changed` reset path.
Also change `FactoryModeController.stop_active_run()` so a successful backend stop request no longer calls `_reset_review_context("active_run_stopped")`; after the request is accepted, the controller must wait for `factory_runtime_stopped` or the normal analysis/review signals to transition state. If the backend stop request returns `False` or raises before any terminal runtime signal, keep controller state/review/display unchanged and return `False`. `TestManager.stop_active_run()` must become the minimal producer for the successful-stop contract in this slice by emitting `FactoryRuntimeStoppedPayload` after successful factory-stop cleanup has fully released runtime ownership. Task 4 will build on that producer to branch successful stops into plain `stopped` versus stop-triggered auto-analysis.
Update `refresh_selection()` / `_refresh_selection_now()` so a same-tuple background refresh keeps the stopped display context and existing review payload instead of rebuilding an empty armed/not-ready state.

- [ ] **Step 4: Run the controller/runtime/workorder/control regression slice**

Run: `pytest tests/test_service_container.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_cleanup_and_abort.py tests/test_factory_mode_controller/test_readiness_and_selection.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_test_manager/test_stop_behavior.py tests/test_workorder_module.py tests/test_control_module.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services_container.py app/service/factory_mode_controller.py app/service/analysis/test_manager.py ui/modules/workorder.py ui/modules/control.py tests/test_service_container.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_cleanup_and_abort.py tests/test_factory_mode_controller/test_readiness_and_selection.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_test_manager/test_stop_behavior.py tests/test_workorder_module.py tests/test_control_module.py
git commit -m "feat(factory-state): add stopped runtime phase"
```

### Task 4: Teach `TestManager` to Branch Manual Stop Outcomes

**Files:**
- Modify: `app/service/analysis/test_manager.py`
- Modify: `tests/test_test_manager/_support.py`
- Test: `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- Test: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`
- Test: `tests/test_test_manager/test_stop_behavior.py`
- Test: `tests/test_test_manager/test_late_attach_buffer_slice.py`
- Test: `tests/test_test_manager/test_review_payload_emission.py`
- Test: `tests/test_test_manager/test_max_duration.py`
- Test: `tests/test_test_manager/test_rejudge.py`
- Test: `tests/test_test_manager/test_user_stop_partial_save.py`
- Test: `tests/test_test_manager/test_shared_recorder.py`
- Test: `tests/test_test_manager/test_signal_disconnection.py`

- [ ] **Step 1: Write the failing stop-branching tests**

```python
def test_stop_active_run_emits_factory_runtime_stopped_when_partial_capture_is_not_eligible(monkeypatch):
    session._config["allow_incomplete_recording_analysis"] = False
    stopped = mgr.stop_active_run(config_id=1)
    assert stopped is True
    payload = bus.factory_runtime_stopped.emit.call_args.args[0]
    assert payload.run_id == "run-1"
    bus.factory_runtime_aborted.emit.assert_not_called()
    bus.test_finished.emit.assert_not_called()
    bus.result_changed.emit.assert_not_called()
    assert "Idle" not in [args[0] for args, _ in bus.status.emit.call_args_list]
    assert 1 not in mgr._accepted_factory_stops


def test_stop_active_run_emits_factory_runtime_stopped_when_toggle_disabled_even_if_window_end_is_captured(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": False,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    recorder._sample_rate = 1000.0
    recorder.get_buffer_from.return_value = np.zeros((4000, 1), dtype=np.float32)

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_stopped.emit.assert_called_once()
    bus.factory_runtime_analyzing_entered.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_stop_active_run_unknown_captured_duration_falls_back_to_factory_runtime_stopped(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    recorder._sample_rate = None

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_stopped.emit.assert_called_once()
    bus.factory_runtime_analyzing_entered.emit.assert_not_called()


def test_stop_active_run_below_window_end_with_flag_enabled_falls_back_to_factory_runtime_stopped(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    recorder._sample_rate = 1000.0
    recorder.get_buffer_from.return_value = np.zeros((2500, 1), dtype=np.float32)

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_stopped.emit.assert_called_once()
    bus.factory_runtime_analyzing_entered.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_stop_active_run_reuses_auto_analysis_when_default_window_end_is_captured(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    mgr.stop_active_run(config_id=1)
    bus.factory_runtime_analyzing_entered.emit.assert_called_once()
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()
    bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_called_once()
    bus.factory_runtime_stopped.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_stop_triggered_auto_analysis_updates_session_buffer_and_sample_rate(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    recorder._sample_rate = 1000.0
    captured = np.zeros((4000, 1), dtype=np.float32)
    recorder.get_buffer_from.return_value = captured

    mgr.stop_active_run(config_id=1)

    assert session._buffer is captured
    assert session._sample_rate == 1000.0


def test_stop_triggered_auto_analysis_clears_accepted_stop_marker(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })

    mgr.stop_active_run(config_id=1)

    assert 1 not in mgr._accepted_factory_stops


def test_stop_active_run_exact_window_end_boundary_is_analysis_eligible(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    recorder._sample_rate = 1000.0
    recorder.get_buffer_from.return_value = np.zeros((3000, 1), dtype=np.float32)

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_analyzing_entered.emit.assert_called_once()
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()
    bus.factory_runtime_stopped.emit.assert_not_called()


def test_stop_triggered_auto_analysis_preserves_original_trigger_source(monkeypatch):
    session._workorder["trigger_source"] = "plc"
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })

    mgr.stop_active_run(config_id=1)

    review_payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
    assert review_payload.trigger_source == "plc"


def test_stop_triggered_auto_analysis_success_reaches_controller_reviewing_state(monkeypatch):
    controller, services, reset_reasons, reviews, _, recorder_cls = (
        _make_controller_with_real_manager(
            monkeypatch,
            allow_incomplete_recording_analysis=True,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            captured_samples=4000,
        )
    )

    stopped = controller.stop_active_run()

    assert stopped is True
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "manual"
    assert controller.review_payload.displayed_config_id == 101
    assert reset_reasons == []
    assert reviews[-1].displayed_config_id == 101


def test_stop_triggered_auto_analysis_no_result_still_enters_reviewing_flow(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    session._run_judgment.side_effect = ValueError("analysis window not fully captured")

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_analyzing_entered.emit.assert_called_once()
    bus.factory_runtime_auto_analysis_no_result.emit.assert_called_once()
    bus.factory_runtime_stopped.emit.assert_not_called()


def test_stop_triggered_auto_analysis_error_emits_review_error_flow(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": True,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })
    session._run_judgment.side_effect = RuntimeError("analysis exploded")

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_analyzing_entered.emit.assert_called_once()
    bus.factory_runtime_auto_analysis_error.emit.assert_called_once()
    bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_called_once()
    bus.factory_runtime_stopped.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_stop_accepted_while_running_blocks_late_recording_done_from_starting_analysis(monkeypatch):
    session._config.update({
        "allow_incomplete_recording_analysis": False,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 1000,
    })

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is True
    mgr._on_recording_done(1)
    bus.factory_runtime_analyzing_entered.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()


def test_factory_runtime_stopped_payload_preserves_original_trigger_source(monkeypatch):
    session._workorder["trigger_source"] = "scan"
    session._config["allow_incomplete_recording_analysis"] = False

    mgr.stop_active_run(config_id=1)

    stopped_payload = bus.factory_runtime_stopped.emit.call_args.args[0]
    assert stopped_payload.trigger_source == "scan"


def test_plain_stopped_stop_does_not_promote_session_into_completed_sessions(monkeypatch):
    session._config["allow_incomplete_recording_analysis"] = False

    mgr.stop_active_run(config_id=1)

    assert 1 not in mgr._completed_sessions


def test_stop_during_manual_reanalysis_remains_unsupported():
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")
    controller.begin_reanalysis(1.0, 3.0)

    stopped = controller.stop_active_run()

    assert stopped is False
    services.test.stop_active_run.assert_not_called()


def test_stop_during_analyzing_emits_factory_runtime_stopped_and_suppresses_late_completion():
    entered_analysis = Event()
    release_analysis = Event()

    def _blocking_judgment():
        entered_analysis.set()
        assert release_analysis.wait(timeout=1)
        raise RuntimeError("late analysis should be suppressed")

    session._run_judgment = _blocking_judgment
    worker = Thread(target=mgr._on_session_done, args=(1,))
    worker.start()
    assert entered_analysis.wait(timeout=1)

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is True
    bus.factory_runtime_stopped.emit.assert_called_once()

    release_analysis.set()
    worker.join(timeout=1)

    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_error.emit.assert_not_called()


def test_stop_during_analyzing_skips_partial_capture_and_save():
    entered_analysis = Event()
    release_analysis = Event()

    def _blocking_judgment():
        entered_analysis.set()
        assert release_analysis.wait(timeout=1)
        return None

    session._run_judgment = _blocking_judgment
    worker = Thread(target=mgr._on_session_done, args=(1,))
    worker.start()
    assert entered_analysis.wait(timeout=1)

    mgr.stop_active_run(config_id=1)

    recorder.get_buffer_from.assert_not_called()
    session._save_wav.assert_not_called()
    release_analysis.set()
    worker.join(timeout=1)


def test_stop_during_analyzing_suppresses_generic_completion_emissions():
    entered_analysis = Event()
    release_analysis = Event()

    def _blocking_judgment():
        entered_analysis.set()
        assert release_analysis.wait(timeout=1)
        return None

    session._run_judgment = _blocking_judgment
    worker = Thread(target=mgr._on_session_done, args=(1,))
    worker.start()
    assert entered_analysis.wait(timeout=1)

    mgr.stop_active_run(config_id=1)

    release_analysis.set()
    worker.join(timeout=1)

    bus.test_finished.emit.assert_not_called()
    bus.result_changed.emit.assert_not_called()
    assert "Idle" not in [args[0] for args, _ in bus.status.emit.call_args_list]
    assert 1 not in mgr._completed_sessions
    assert 1 not in mgr._accepted_factory_stops


def test_stop_active_run_shared_recorder_keeps_surviving_owner_connected(monkeypatch):
    other_session = MagicMock()
    other_session.config_id = 2
    mgr._sessions[2] = other_session
    mgr._recorders[2] = recorder

    mgr.stop_active_run(config_id=1)

    assert mgr._sessions == {2: other_session}
    assert mgr._recorders == {2: recorder}
    assert recorder.stop_calls == 0


def test_stop_active_run_cleanup_failure_preserves_shared_recorder_for_surviving_owner(monkeypatch):
    other_session = MagicMock()
    other_session.config_id = 2
    mgr._sessions[2] = other_session
    mgr._recorders[2] = recorder

    recorder.stop.side_effect = RuntimeError("stop exploded")

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is False
    assert mgr._sessions == {2: other_session}
    assert mgr._recorders == {2: recorder}
    assert mgr._shared_recorders


def test_stop_active_run_disconnects_callbacks_before_freezing_display(monkeypatch):
    mgr.stop_active_run(config_id=1)

    recorder.chunk_ready.disconnect.assert_any_call(session._chunk_conn)
    recorder.recording_done.disconnect.assert_any_call(session._done_lambda)
    recorder.error_occurred.disconnect.assert_any_call(session._error_lambda)
    assert bus.stft_updated.emit.call_count == 0
    assert bus.recording_progress.emit.call_count == 0


def test_factory_runtime_stopped_emits_only_after_runtime_release(monkeypatch):
    emission_states = []

    def _capture(payload):
        emission_states.append(
            {
                "payload": payload,
                "sessions": dict(mgr._sessions),
                "recorders": dict(mgr._recorders),
                "monitors": dict(mgr._monitors),
                "is_running": mgr.is_running(config_id=1),
            }
        )

    bus.factory_runtime_stopped.emit.side_effect = _capture

    mgr.stop_active_run(config_id=1)

    assert len(emission_states) == 1
    assert emission_states[0]["sessions"] == {}
    assert emission_states[0]["recorders"] == {}
    assert emission_states[0]["monitors"] == {}
    assert emission_states[0]["is_running"] is False


def test_stop_active_run_partial_buffer_capture_failure_falls_back_to_factory_runtime_stopped():
    recorder.get_buffer_from.side_effect = RuntimeError("buffer exploded")
    mgr.stop_active_run(config_id=1)
    bus.factory_runtime_stopped.emit.assert_called_once()


def test_stop_active_run_partial_wav_save_failure_falls_back_to_factory_runtime_stopped(monkeypatch):
    session._save_wav.side_effect = RuntimeError("disk full")

    mgr.stop_active_run(config_id=1)

    bus.factory_runtime_stopped.emit.assert_called_once()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_stop_active_run_cleanup_failure_still_emits_factory_runtime_aborted(monkeypatch):
    recorder.stop.side_effect = RuntimeError("stop exploded")

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is False
    bus.factory_runtime_aborted.emit.assert_called_once()
    assert 1 not in mgr._accepted_factory_stops
```

- [ ] **Step 2: Run the backend stop slice**

Run: `pytest tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_shared_recorder.py tests/test_test_manager/test_signal_disconnection.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -k "stop_active_run or factory_runtime_stopped or incomplete or cleanup_failure or stop_triggered_auto_analysis or stop_during_manual_reanalysis or user_stop or shared_recorder or signal_disconnection or max_duration" -v`
Expected: FAIL because Task 3 only established the minimal `factory_runtime_stopped` contract. Stop still does not branch across the full eligibility matrix, does not reuse the success/no-result/error stop-triggered analysis lifecycle, does not yet record accepted stop state early enough to win the `running` stop-vs-completion race, does not yet guarantee that `analyzing` stops skip partial capture / partial-WAV work, and still leaks generic completion/session-finalization behavior into cancel-to-`stopped` paths.

- [ ] **Step 3: Implement the phase-aware stop flow**

```python
def stop_active_run(self, config_id: int | None = None) -> bool:
    target_config_id = next(iter(self._sessions)) if config_id is None else config_id
    if target_config_id not in self._sessions:
        return False
    return self._stop_factory_run(target_config_id)


def _stop_factory_run(self, config_id: int) -> bool:
    session = self._sessions.get(config_id)
    if session is None:
        return False
    if config_id in self._accepted_factory_stops:
        return True
    self._accepted_factory_stops.add(config_id)
    if self._session_is_analyzing(config_id):
        return self._stop_to_stopped(config_id, session, reason="analyzing-stop")
    return self._stop_recording_with_optional_analysis(config_id, session)


def _stop_recording_with_optional_analysis(self, config_id: int, session) -> bool:
    partial_buf, partial_wav_ok, captured_duration_sec = self._capture_partial_stop_buffer(
        config_id,
        session,
    )
    if self._stop_is_analysis_eligible(
        session,
        captured_duration_sec=captured_duration_sec,
        partial_wav_ok=partial_wav_ok,
    ):
        self._run_auto_analysis_on_captured_stop_buffer(
            config_id,
            session,
            partial_buf=partial_buf,
        )
        return True
    self._emit_optional_signal(
        "factory_runtime_stopped",
        self._make_factory_runtime_stopped_payload(config_id, session),
    )
    return True


def _run_auto_analysis_on_captured_stop_buffer(
    self,
    config_id: int,
    session,
    *,
    partial_buf: np.ndarray | None,
) -> None:
    self._emit_optional_signal(
        "factory_runtime_analyzing_entered",
        self._make_factory_runtime_config_payload(config_id, self._factory_run_id(session)),
    )
    result = session._run_judgment()
    self._finalize_auto_analysis_result(
        config_id,
        session,
        result=result,
        captured_buffer=partial_buf,
    )
```

Make the eligibility helper explicitly gate on `allow_incomplete_recording_analysis`, inclusive default-window end capture, and successful partial WAV persistence. Record stop acceptance before branching so `_on_recording_done()` / `_on_session_done()` can short-circuit if stop wins while the runtime is still `running`. Disconnect the stopping session's recorder/monitor callbacks before cleanup so late live-stream frame/progress updates stop and the UI stays frozen at the stop moment. Keep the `analyzing` stop path separate from the recording-stop path so it never calls `recorder.get_buffer_from()` or `session._save_wav()`. On cancel-to-`stopped` paths, suppress both factory review signals and the generic completion emissions (`test_finished`, `result_changed`, terminal `Idle` status), and do not populate `_completed_sessions`. Apply the shared-recorder ownership rule when stopping: detach only the stopping config, keep the recorder alive for surviving owners, and stop/discard it only when the last owner exits; the same ownership rule must continue to hold when cleanup fails. Return `False` when cleanup fails and no successful stop outcome is accepted. Keep true cleanup failures on the abort path, but downgrade buffer-capture or partial-WAV-save failures to the normal stopped path when orderly stop cleanup still succeeds.
For eligible running-stop analysis, explicitly copy the captured slice into `session._buffer` and sync `session._sample_rate` from the recorder before reusing the normal judgment pipeline.
Treat unknown captured duration exactly like the other benign ineligible cases: log it, skip auto-analysis, and emit `factory_runtime_stopped`. Clear the config-keyed accepted-stop marker on every terminal stop path: plain `factory_runtime_stopped`, `factory_runtime_aborted`, and stop-triggered analysis settlement. When stop-triggered auto-analysis does run, preserve the full automatic review lifecycle for success, no-result, and error outcomes; analysis exceptions on the eligible stop path should emit `factory_runtime_auto_analysis_error` (plus the existing cleanup-settled signal) instead of being downgraded to `factory_runtime_stopped` or `factory_runtime_aborted`. At least one controller-level integration test in this task should prove that an eligible stop-triggered analysis drives the normal backend-to-controller handoff all the way into controller phase `reviewing`, not just that the `TestManager` emits the raw success signal. Emit `factory_runtime_stopped` only after the runtime has fully released ownership for that config in `_sessions`, `_recorders`, and `_monitors`, so controller readiness observes the backend as idle during the stop signal callback. Preserve the unchanged natural full-duration completion path and prove it with an existing completion-path regression such as `tests/test_test_manager/test_max_duration.py`. Build any stop-triggered review payload from the original run metadata so `trigger_source` stays `manual` / `plc` / `scan` from run start instead of becoming a stop-specific value.

- [ ] **Step 4: Run the full `TestManager` stop/review regression suite**

Run: `pytest tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_shared_recorder.py tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_rejudge.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_manager.py tests/test_test_manager/_support.py tests/test_factory_mode_controller/test_reanalysis_and_review.py tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_shared_recorder.py tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_rejudge.py tests/test_factory_mode_controller/test_stop_and_active_run.py
git commit -m "feat(test-manager): branch manual stop into stopped or analysis"
```

### Task 5: Preserve the Stopped Display Across the Analysis UI

**Files:**
- Modify: `app/main_window.py`
- Modify: `ui/modules/result.py`
- Modify: `ui/modules/stft.py`
- Modify: `ui/modules/spectrum.py`
- Modify: `ui/modules/spl.py`
- Modify: `ui/modules/loudness.py`
- Modify: `ui/modules/octave.py`
- Test: `tests/test_result_module.py`
- Test: `tests/test_stft_module/test_review_window_controls.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing stopped-display tests**

```python
def test_result_module_keeps_in_progress_badge_when_plain_stop_preserves_display(qapp):
    module.services.bus.factory_state_changed.emit(_state("running", displayed_config_id=101, active_config_id=101))
    module.services.bus.factory_state_changed.emit(_state("stopped", displayed_config_id=101))
    assert module.lb_overall.text() == "分析中"


def test_result_module_preserves_existing_summary_card_when_plain_stop_enters_stopped(qapp):
    services.bus.factory_review_changed.emit(_review_payload_with_summary(displayed_config_id=101))
    services.bus.factory_state_changed.emit(_state("reviewing", displayed_config_id=101))
    services.bus.factory_state_changed.emit(_state("stopped", displayed_config_id=101))
    assert module.lb_overall.text() == "PASS"


def test_stft_keeps_rendering_in_stopped_phase_but_hides_manual_controls(qapp):
    _emit_phase(services, payload, run_phase="stopped")
    assert module.current_image_payload() is not None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False


def test_stft_preserved_review_content_in_plain_stopped_still_hides_manual_controls(qapp):
    payload = _make_review_payload(
        judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)}
    )
    _emit_phase(services, payload, run_phase="reviewing")
    _emit_phase(services, payload, run_phase="stopped")
    assert module.current_image_payload() is not None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False


def test_stft_shows_review_controls_for_stop_triggered_no_result_reviewing(qapp):
    _emit_phase(services, payload, run_phase="reviewing", no_result=True)
    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True


def test_committed_chart_modules_keep_review_payload_visible_in_stopped_phase(qapp):
    services.bus.factory_review_changed.emit(_review_payload_with_chart_payload(101, channels=(0, 1)))
    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_state_changed.emit(_state(101, "stopped"))
    assert module.has_committed_plot_data() is True


def test_committed_chart_modules_remain_blank_in_stopped_phase_without_review_payload(qapp):
    services.bus.factory_state_changed.emit(_state(101, "running"))
    services.bus.factory_state_changed.emit(_state(101, "stopped"))
    assert module.has_committed_plot_data() is False


def test_main_window_analysis_toolbar_remains_visible_in_stopped_phase(qapp):
    services.bus.factory_state_changed.emit(_stopped_state(101))
    assert window.analysis_toolbar.isVisible() is True


def test_main_window_stopped_state_preserves_review_channel_resolution(qapp):
    services.bus.factory_review_changed.emit(_review_payload_with_chart_payload(101, channels=(0, 2)))
    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    _select_toolbar_channel(window, qapp, 2)

    services.bus.factory_state_changed.emit(_state(101, "stopped"))

    assert window.analysis_toolbar.current_channel_index() == 2
    _assert_committed_chart_channel(window, channel_index=2)
```

- [ ] **Step 2: Run the UI preservation slice**

Run: `pytest tests/test_result_module.py tests/test_stft_module/test_review_window_controls.py tests/test_stft_module/test_live_review_rendering.py tests/test_stft_module/test_reanalysis_and_reset.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py -k "stopped or review or reanalysis" -v`
Expected: FAIL because stopped is not a recognized display phase and the modules clear or hide content.

- [ ] **Step 3: Implement the display-preservation rules**

```python
MainWindow._DISPLAY_CHANNEL_PHASES = frozenset({"running", "analyzing", "reviewing", "reanalyzing", "stopped"})

StftModule._DISPLAY_PHASES = frozenset({"running", "analyzing", "reviewing", "reanalyzing", "stopped"})

class ResultModule(QWidget):
    _DISPLAY_PRESERVING_PHASES = frozenset({"running", "analyzing", "reanalyzing", "stopped"})

    def _is_in_progress(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._DISPLAY_PRESERVING_PHASES
            and getattr(self._review_payload, "summary_payload", None) is None
        )


class SpectrumModule(QWidget):
    _RENDER_PHASES = frozenset({"reviewing", "stopped"})
```

Mirror the same `_RENDER_PHASES` change in `ui/modules/spl.py`, `ui/modules/loudness.py`, and `ui/modules/octave.py`.
Keep `_resolve_display_channels()` / `_resolve_current_display_channel()` behavior consistent in `stopped`: if selected channels exist, keep using them; if the preserved review payload supplies the authoritative chart channels, preserve the remembered review channel when it is still valid and fall back the same way review mode already does.

- [ ] **Step 4: Run the affected UI and integration regression suites**

Run: `pytest tests/test_result_module.py tests/test_stft_module/test_review_window_controls.py tests/test_stft_module/test_live_review_rendering.py tests/test_stft_module/test_reanalysis_and_reset.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py tests/test_workorder_module.py tests/test_control_module.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main_window.py ui/modules/result.py ui/modules/stft.py ui/modules/spectrum.py ui/modules/spl.py ui/modules/loudness.py ui/modules/octave.py tests/test_result_module.py tests/test_stft_module/test_review_window_controls.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py
git commit -m "feat(factory-ui): preserve stopped display context"
```

## Final Verification

- [ ] Run: `pytest tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_service_container.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_cleanup_and_abort.py tests/test_factory_mode_controller/test_readiness_and_selection.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_factory_mode_controller/test_reanalysis_and_review.py tests/test_workorder_module.py tests/test_control_module.py tests/test_test_manager/test_stop_behavior.py tests/test_test_manager/test_late_attach_buffer_slice.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/test_max_duration.py tests/test_test_manager/test_user_stop_partial_save.py tests/test_test_manager/test_shared_recorder.py tests/test_test_manager/test_signal_disconnection.py tests/test_test_manager/test_rejudge.py tests/test_result_module.py tests/test_stft_module/test_review_window_controls.py tests/test_stft_module/test_live_review_rendering.py tests/test_stft_module/test_reanalysis_and_reset.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py -v`
- [ ] Expect: PASS
- [ ] If a focused integration regression fails, fix it in the task that introduced the regression before moving on. Do not stack speculative cleanup on top of a red suite.

## Execution Notes

- Execute from an isolated git worktree rooted at `.worktrees/<branch-name>`.
- Keep one implementer subagent active at a time for plan tasks.
- After each implementation task, run the paired `spec-code-reviewer` / `quality-code-reviewer` loop before advancing.
- Preserve unrelated dirty files from the parent branch; only stage files listed in each task’s commit step.
