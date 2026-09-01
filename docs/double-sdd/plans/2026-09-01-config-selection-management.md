# 配置选择迁移到配置管理 Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove configuration selection from the test homepage and let administrators or engineers edit the persisted ordered production selection through a modal launched from configuration management.

**Architecture:** `FactoryModeController` becomes the sole application service for startup selection initialization and confirmed selection commits, returning a structured result so persistence failures never leak partial runtime state. A new modal owns only draft UI state and reuses `FactoryConfigListWidget`; `ConfigModule` launches it, while `WorkOrderModule` becomes a read-only projection of the controller's authoritative selection.

**Tech Stack:** Python 3, PyQt6, pytest, existing `FactorySelectionSettings`, `FactoryModeController`, and Qt signal bus.

---

## File Structure

- Modify `app/service/factory_mode_controller.py`: define the structured apply result, one-time startup initialization, validation/locking, transactional confirmed selection application, pending-restart handling, and PLC reset dispatch.
- Modify `app/services_container.py`: invoke the controller's idempotent selection initializer exactly once after controller construction.
- Create `ui/popups/factory_config_selection_dialog.py`: modal draft editor with empty state, locking, conflict refresh, inline messages, and confirm/cancel semantics.
- Modify `ui/modules/config.py`: add the global “选择使用配置” launcher and open the modal independently of the current config editor.
- Modify `ui/modules/workorder.py`: remove the homepage selector and persistence UI; read selection exclusively from factory state.
- Modify `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`: cover structured confirmed commits and pending restart behavior.
- Modify `tests/test_service_container.py`: cover the sole startup initialization call.
- Create `tests/test_factory_config_selection_dialog.py`: cover modal draft, empty, lock, commit, cancel, and external refresh behavior.
- Modify `tests/test_config_module.py`: cover the launcher and editor independence.
- Modify `tests/test_workorder_module.py`: replace selector-owned behavior tests with read-only projection and no-homepage-selector tests.
- Modify `tests/test_control_module.py`: update integrations that currently mutate `WorkOrderModule.config_selector` to confirm through the controller/dialog boundary.
- Modify `tests/multi_run_integration/test_composition_and_ingress.py`: update the UI integration assertion to use the new confirmed-selection entry point.
- Modify `tests/test_main_window.py`: verify role-gated access and absence of a homepage selection control.

### Task 1: Centralize selection initialization and confirmed commits

**Files:**
- Modify: `app/service/factory_mode_controller.py`
- Modify: `app/services_container.py`
- Modify: `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- Modify: `tests/test_service_container.py`

- [ ] **Step 1: Write failing tests for the result contract and transactional commit**

Add focused tests proving:

```python
def test_confirm_selection_persists_before_runtime_state_and_resets_only_added_configs():
    result = controller.confirm_selection((101, 202))
    assert result.status == "applied"
    assert result.selected_config_ids == (101, 202)
    assert operation_order == [
        ("persist", (101, 202)),
        ("reset", (202,)),
        ("state", (101, 202)),
    ]


def test_confirm_selection_rejects_persistence_failure_without_runtime_side_effects():
    result = controller.confirm_selection((202,))
    assert result.status == "rejected"
    assert result.message == "配置保存失败"
    assert controller.state.selected_config_ids == (101,)
    assert reset_calls == []
    assert state_events == []


def test_confirm_selection_outside_prepared_routes_returns_restart_required():
    result = controller.confirm_selection((202,))
    assert result.status == "restart_required"
    assert settings.load_pending_selected_config_ids() == (202,)
    assert controller.state.selected_config_ids == (202,)
    assert controller.state.start_ready is False


def test_confirm_selection_pending_write_failure_has_no_runtime_or_workorder_side_effects():
    previous_state = controller.state
    previous_pending = settings.load_pending_selected_config_ids()
    result = controller.confirm_selection((202,))
    assert result.status == "rejected"
    assert result.message == "下次启动配置保存失败"
    assert controller.state is previous_state
    assert settings.load_pending_selected_config_ids() == previous_pending
    assert state_events == []
    assert workorder_events == []
    assert reset_calls == []


def test_confirm_selection_unchanged_has_no_side_effects():
    result = controller.confirm_selection((101,))
    assert result.status == "unchanged"
    assert persist_calls == []
    assert reset_calls == []
```

Also test rejection for empty, duplicate, non-positive, missing IDs, busy phases (`running`, `analyzing`, `reanalyzing`), and `start_ready_reason == "启动请求处理中"`.

- [ ] **Step 2: Run the controller tests to verify they fail**

Run: `python -m pytest tests/test_factory_mode_controller/test_pending_start_lifecycle.py -q`

Expected: FAIL because `FactorySelectionApplyResult`, `confirm_selection`, and startup initialization do not exist.

- [ ] **Step 3: Implement the structured result and confirmed commit entry point**

Add an immutable exact-type result near the controller:

```python
@dataclass(frozen=True, slots=True)
class FactorySelectionApplyResult:
    status: str
    selected_config_ids: tuple[int, ...]
    message: str = ""
```

Implement `confirm_selection()` with this order:

```python
def confirm_selection(self, selected_config_ids) -> FactorySelectionApplyResult:
    selected = self._validate_confirmed_selection(selected_config_ids)
    if selected is None:
        return self._selection_result("rejected", message="配置选择无效")
    if self._selection_edit_locked():
        return self._selection_result("rejected", message=self._selection_lock_reason())
    if selected == tuple(self._state.selected_config_ids):
        return self._selection_result("unchanged")

    old_selected = tuple(self._state.selected_config_ids)
    if self._requires_pending_restart(selected):
        error = self._save_pending_selection(selected)
        if error:
            self.bus.factory_selection_notice.emit(error)
            return self._selection_result("rejected", message=error)
        self._apply_pending_selection_state(selected)
        self._request_added_plc_resets(old_selected, selected)
        return self._selection_result(
            "restart_required",
            message=PRODUCTION_CONFIG_RESTART_REQUIRED_NOTICE,
        )

    error = self._save_authoritative_selection(selected)
    if error:
        self.bus.factory_selection_notice.emit(error)
        return self._selection_result("rejected", message=error)
    self._request_added_plc_resets(old_selected, selected)
    self.set_selection(selected)
    self.bus.factory_selection_notice.emit("")
    return self._selection_result("applied")
```

Use internal helpers rather than exposing `_state` mutation. Validate requested IDs against `config_service.list_configs()`. `_save_pending_selection()` must return `None` on verified success or the exact user-facing error string on failure and must not mutate controller state; `_apply_pending_selection_state()` may run only after that helper succeeds. Refactor existing `_set_pending_selection()` callers to compose those same two helpers so there is one unambiguous pending-write contract and no stale global notice string.

- [ ] **Step 4: Write failing tests for unique idempotent startup initialization**

Cover both prepared and unprepared service contexts:

```python
def test_initialize_selection_once_resets_prepared_selection_once():
    first = controller.initialize_selection()
    second = controller.initialize_selection()
    assert first.selected_config_ids == (101, 202)
    assert second is first
    assert reset_calls == [(101, 202)]
    assert persistence_calls == []
    assert state_events == []


def test_service_container_initializes_factory_selection_once_after_controller_creation():
    assert initialize_calls == [()]
```

For the unprepared context, assert the first call performs exactly one necessary restore/persistence sequence and one state publication, while the second call adds no persistence calls, state events, or reset requests. For the prepared context, assert construction already supplied the authoritative state and initialization adds only the single reset request, not another state event or persistence write.

- [ ] **Step 5: Run the initialization tests to verify they fail**

Run: `python -m pytest tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_service_container.py -q`

Expected: FAIL because startup initialization is not yet owned by the controller/container.

- [ ] **Step 6: Implement the sole startup initializer and composition-root call**

Add an `_selection_initialized` flag and `initialize_selection()`:

```python
def initialize_selection(self) -> FactoryControllerState:
    if self._selection_initialized:
        return self._state
    self._selection_initialized = True
    if getattr(self.services, "production_selection_locked", False):
        selected = tuple(self._state.selected_config_ids)
    else:
        selected, had_missing = self._load_reconciled_startup_selection()
        self.restore_selection(selected, had_missing_configs=had_missing)
    self._request_added_plc_resets((), selected)
    return self._state
```

Call `self.factory_mode.initialize_selection()` once in `ServiceContainer.__post_init__` immediately after `_preserve_production_unavailable_state()` and before any UI can be constructed. Ensure repeated calls are side-effect free.

- [ ] **Step 7: Run focused tests**

Run: `python -m pytest tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_service_container.py tests/test_factory_selection_settings.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the task checkpoint**

```bash
git add app/service/factory_mode_controller.py app/services_container.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_service_container.py
git commit -m "tmp/sdd: centralize factory selection commits"
```

### Task 2: Build the modal draft editor

**Files:**
- Create: `ui/popups/factory_config_selection_dialog.py`
- Create: `tests/test_factory_config_selection_dialog.py`
- Reuse without structural changes: `ui/widgets/factory_config_list.py`

- [ ] **Step 1: Write failing modal behavior tests**

Create a minimal service/bus fixture and cover:

```python
def test_dialog_empty_state_keeps_launcher_flow_visible_but_disables_confirm(qapp):
    dialog = FactoryConfigSelectionDialog(empty_services)
    assert dialog.empty_label.text() == "配置列表为空，请先新建配置。"
    assert dialog.empty_label.isVisible()
    assert dialog.confirm_button.isEnabled() is False


def test_dialog_nonempty_rows_allow_empty_draft_to_validate_on_confirm(qapp):
    dialog = FactoryConfigSelectionDialog(services_with_configs_but_no_selection)
    assert dialog.confirm_button.isEnabled() is True
    dialog.confirm_button.click()
    assert dialog.notice_label.text() == "请至少选择一个配置。"
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert services.factory_mode.confirm_calls == []


def test_dialog_cancel_discards_draft_without_commit(qapp):
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    dialog.reject()
    assert services.factory_mode.confirm_calls == []


def test_dialog_confirm_applies_ordered_draft_once(qapp):
    dialog = FactoryConfigSelectionDialog(services)
    dialog.config_selector.set_checked(202, True)
    dialog.config_selector.move_checked_config(202, before_config_id=101)
    dialog.confirm_button.click()
    assert services.factory_mode.confirm_calls == [(202, 101)]


def test_dialog_busy_state_is_read_only_and_explains_lock(qapp):
    services.bus.factory_state_changed.emit(busy_state)
    assert dialog.config_selector.is_locked() is True
    assert dialog.confirm_button.isEnabled() is False
    assert "当前" in dialog.notice_label.text()


def test_external_selection_change_rebuilds_and_warns(qapp):
    dialog.config_selector.set_checked(202, True)
    services.bus.factory_state_changed.emit(external_state)
    assert dialog.config_selector.enabled_config_ids() == external_state.selected_config_ids
    assert dialog.notice_label.text() == "配置选择已更新，请重新确认。"


def test_config_refresh_adds_imported_and_renamed_rows_while_open(qapp):
    config_service.rows = [
        {"config_id": 101, "config_name": "已重命名"},
        {"config_id": 202, "config_name": "新导入配置"},
    ]
    services.bus.config_changed.emit()
    assert dialog.config_selector.count() == 2
    assert rendered_names(dialog.config_selector) == ["已重命名", "新导入配置"]
```

Also cover Escape/window close, rejected result retaining the draft, restart-required result showing the existing restart message, config refresh pruning stale draft IDs, and state changes that only alter run phase preserving the draft.

- [ ] **Step 2: Run the new dialog tests to verify they fail**

Run: `python -m pytest tests/test_factory_config_selection_dialog.py -q`

Expected: FAIL because the dialog module does not exist.

- [ ] **Step 3: Implement the modal**

Create `FactoryConfigSelectionDialog(QDialog)` with:

```python
class FactoryConfigSelectionDialog(QDialog):
    _LOCKED_PHASES = frozenset({"running", "analyzing", "reanalyzing"})
    _PENDING_START_REASON = "启动请求处理中"

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        self._baseline_selected_ids = tuple(services.factory_mode.state.selected_config_ids)
        self.setWindowTitle("配置选择")
        self.instructions_label = QLabel(
            "勾选生产使用的配置，拖拽可调整优先顺序，第一项为首选配置。",
            self,
        )
        self.config_selector = FactoryConfigListWidget(self)
        self.empty_label = QLabel("配置列表为空，请先新建配置。", self)
        self.notice_label = QLabel(self)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.confirm_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setText("确定")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
```

Add `prepare_for_open()` so a reused dialog refreshes its rows, authority baseline, notices, and lock state immediately before every `exec()`. Enable confirmation whenever at least one configuration row exists and editing is unlocked, even if the draft has no checked item; `_confirm()` must then show “请至少选择一个配置。” and remain open for an empty draft. Only an entirely empty configuration list or a locked state disables confirmation. Connect confirmation through a custom `_confirm()` and call `accept()` only for `applied`, `restart_required`, or `unchanged`. Connect bus signals defensively because tests may use stubs. A hidden singleton remains connected so config/state updates are available before its next open; `prepare_for_open()` remains the authoritative refresh boundary.

- [ ] **Step 4: Run modal and widget tests**

Run: `python -m pytest tests/test_factory_config_selection_dialog.py tests/test_factory_config_list.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the task checkpoint**

```bash
git add ui/popups/factory_config_selection_dialog.py tests/test_factory_config_selection_dialog.py
git commit -m "tmp/sdd: add factory selection dialog"
```

### Task 3: Add the configuration-management launcher

**Files:**
- Modify: `ui/modules/config.py`
- Modify: `tests/test_config_module.py`

- [ ] **Step 1: Write failing launcher tests**

Add tests that assert:

```python
def test_factory_selection_launcher_is_always_available(qapp):
    module = _make_module(configs=[])
    assert module.btn_select_active_configs.text() == "选择使用配置"
    assert module.btn_select_active_configs.isEnabled() is True


def test_factory_selection_launcher_opens_modal_without_resolving_dirty_editor(qapp, monkeypatch):
    module = _make_module()
    module.spin_record.setValue(module.spin_record.value() + 1.0)
    opened = []
    monkeypatch.setattr(
        "ui.modules.config.FactoryConfigSelectionDialog.exec",
        lambda dialog: opened.append(dialog),
    )
    module.btn_select_active_configs.click()
    assert opened
    assert module._has_unsaved_changes() is True


def test_factory_selection_launcher_reuses_one_modal_instance(qapp, monkeypatch):
    module = _make_module()
    identities = []
    monkeypatch.setattr(
        "ui.modules.config.FactoryConfigSelectionDialog.exec",
        lambda dialog: identities.append(id(dialog)),
    )
    module.btn_select_active_configs.click()
    module.btn_select_active_configs.click()
    assert len(set(identities)) == 1
```

- [ ] **Step 2: Run focused config tests to verify they fail**

Run: `python -m pytest tests/test_config_module.py -q`

Expected: FAIL because the launcher does not exist.

- [ ] **Step 3: Implement the launcher**

Import the dialog and add a dedicated full-width row below the existing new/delete/import/export row:

```python
self.btn_select_active_configs = QPushButton("选择使用配置")
self.btn_select_active_configs.setObjectName("select_active_factory_configs")
left_lay.addWidget(self.btn_select_active_configs)
```

Create the dialog lazily once and reuse it on later clicks:

```python
def _open_factory_config_selection_dialog(self) -> None:
    if self._factory_selection_dialog is None:
        self._factory_selection_dialog = FactoryConfigSelectionDialog(
            self.services,
            self,
        )
    self._factory_selection_dialog.prepare_for_open()
    self._factory_selection_dialog.exec()
```

Do not call `confirm_leave()`, `_resolve_unsaved_changes_before()`, `_on_save()`, or `_refresh_list()` in this flow.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_config_module.py tests/test_factory_config_selection_dialog.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the task checkpoint**

```bash
git add ui/modules/config.py tests/test_config_module.py
git commit -m "tmp/sdd: launch selection from config management"
```

### Task 4: Remove selection controls from the homepage projection

**Files:**
- Modify: `ui/modules/workorder.py`
- Modify: `tests/test_workorder_module.py`
- Modify: `tests/test_control_module.py`
- Modify: `tests/multi_run_integration/test_composition_and_ingress.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Replace selector-owned tests with failing projection tests**

Remove tests that drive `workorder.config_selector` or assert its persistence label. Add:

```python
def test_workorder_homepage_has_no_factory_config_selector(qapp):
    module, _ = _make_module(initial_factory_state=selected_state)
    assert hasattr(module, "config_selector") is False
    assert hasattr(module, "factory_config_section") is False
    assert "配置选择" not in [label.text() for label in module.findChildren(QLabel)]


def test_workorder_value_reads_authoritative_factory_state(qapp):
    module, services = _make_module(initial_factory_state=selected_state)
    assert module.value()["selected_config_ids"] == selected_state.selected_config_ids
    services.factory_mode.state = changed_state
    assert module.value()["selected_config_ids"] == changed_state.selected_config_ids
    services.bus.factory_state_changed.emit(changed_state)
    assert module.value()["selected_config_ids"] == changed_state.selected_config_ids


def test_workorder_state_change_republishes_ordered_selection(qapp):
    services.bus.factory_state_changed.emit(changed_state)
    payload = services.bus.workorder_changed.emissions[-1][0][0]
    assert payload["selected_config_ids"] == changed_state.selected_config_ids
```

Update control and composition integration tests to invoke `services.factory_mode.confirm_selection(...)` or emit the resulting authoritative state rather than manipulating a homepage widget.

Add a main-window role test:

```python
def test_operator_has_no_factory_selection_entry_on_test_page(window):
    window.current_user_name = "operator"
    window.current_user_role = "操作员"
    window.set_role()
    assert window.top_button_dict["配置"].isEnabled() is False
    assert hasattr(window.workorder, "config_selector") is False


@pytest.mark.parametrize("role", ["管理员", "工程师"])
def test_privileged_roles_can_access_factory_selection_entry(window, role):
    window.current_user_name = "privileged-user"
    window.current_user_role = role
    window.set_role()
    assert window.top_button_dict["配置"].isEnabled() is True
    assert window.config_module.btn_select_active_configs.isEnabled() is True
```

- [ ] **Step 2: Run homepage and integration tests to verify they fail**

Run: `python -m pytest tests/test_workorder_module.py tests/test_control_module.py tests/multi_run_integration/test_composition_and_ingress.py tests/test_main_window.py -q`

Expected: FAIL because `WorkOrderModule` still renders and owns the selector.

- [ ] **Step 3: Simplify `WorkOrderModule`**

Remove the `FactoryConfigListWidget` import, selection section widgets, startup restoration, selection mutation/persistence methods, selector locking, config-list reload connection, and local selection persistence notice.

Change the payload source:

```python
def value(self) -> dict:
    state = getattr(self.services.factory_mode, "state", FactoryControllerState())
    return {
        "sn": "",
        "model": "",
        "selected_config_ids": tuple(state.selected_config_ids),
    }
```

Keep `_on_factory_state_changed()` / `_sync_factory_state()` responsible for assigning the state, updating the phase summary, and republishing the workorder when the ordered selection changes. Preserve PLC SN/model behavior and manual-judgment display reset behavior.

- [ ] **Step 4: Run the homepage and integration tests**

Run: `python -m pytest tests/test_workorder_module.py tests/test_control_module.py tests/multi_run_integration/test_composition_and_ingress.py tests/test_main_window.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the task checkpoint**

```bash
git add ui/modules/workorder.py tests/test_workorder_module.py tests/test_control_module.py tests/multi_run_integration/test_composition_and_ingress.py tests/test_main_window.py
git commit -m "tmp/sdd: remove homepage config selection"
```

### Task 5: Full regression verification

**Files:**
- Modify only if a verified regression requires an in-scope correction.

- [ ] **Step 1: Run the focused factory/config UI suite**

Run: `python -m pytest tests/test_factory_config_selection_dialog.py tests/test_factory_config_list.py tests/test_factory_mode_controller tests/test_factory_selection_settings.py tests/test_workorder_module.py tests/test_control_module.py tests/test_config_module.py tests/test_main_window.py tests/test_service_container.py tests/multi_run_integration/test_composition_and_ingress.py -q`

Expected: PASS with zero failures.

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Run source compilation**

Run: `python -m compileall -q app ui tests`

Expected: exit code 0 with no syntax errors.

- [ ] **Step 4: Inspect the final diff for scope and whitespace errors**

Run: `git diff --check HEAD~4..HEAD`

Expected: no output and exit code 0.

Run: `git diff --stat HEAD~4..HEAD`

Expected: only the files listed in this plan are changed.

- [ ] **Step 5: Create the final temporary checkpoint if verification required fixes**

```bash
git add <only-in-scope-files>
git commit -m "tmp/sdd: verify config selection migration"
```

Skip this commit if the verification task produces no file changes.
