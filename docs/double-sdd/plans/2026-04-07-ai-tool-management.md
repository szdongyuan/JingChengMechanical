# AI Tool Management Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a top-level `AI工具管理` page that supports global AI toolchain registry CRUD, keeps per-config AI binding in the existing config page, and propagates registry changes safely across the UI.

**Architecture:** Extend `AiService` to own toolchain-registry CRUD and binding-impact helpers, add an `ai_toolchains_changed` bus signal for UI refresh, implement a new master/detail PyQt module for toolchain management, and wire it into `MainWindow` as a role-gated top-level tab. Keep rename/delete behavior transactional so binding rows stay coherent and history rows remain untouched.

**Tech Stack:** Python, PyQt6, Peewee ORM, pytest

---

**Spec:** `docs/double-sdd/specs/2026-04-07-ai-tool-management-design.md`

## File Map

- `app/service/ai_service.py`
  Own global AI toolchain registry CRUD, rename/delete binding rewrites, and registry validation helpers alongside the existing binding/runtime APIs.

- `app/services_container.py`
  Add the `ai_toolchains_changed` event-bus signal used by live UI modules.

- `ui/modules/config.py`
  Refresh enabled toolchain options when the registry changes, preserve unsaved local AI edits, and reject stale toolchain selections at config-save time.

- `ui/modules/ai_tool_management.py`
  New master/detail registry-management page for create, edit, enable/disable, rename, delete, empty state, and dirty-state prompts.

- `app/main_window.py`
  Add the `AI工具管理` top-level tab, page builder, and role-permission wiring.

- `tests/test_ai_service.py`
  Extend service-level coverage for registry CRUD, rename binding rewrites, delete binding clearing, and fixed runtime fields.

- `tests/test_config_module.py`
  Add coverage for `ai_toolchains_changed` handling, dirty AI-binding preservation, and stale toolchain validation during config save.

- `tests/test_ai_tool_management_module.py`
  New widget-level coverage for the management page layout, CRUD flows, dirty-state prompts, and delete confirmation behavior.

- `tests/test_main_window.py`
  Extend navigation/role tests for the new top-level tab and module wiring.

## Task 1: Extend `AiService` With Toolchain Registry CRUD

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `tests/test_ai_service.py`

- [ ] **Step 1: Write the failing registry CRUD tests**

Add focused tests in `tests/test_ai_service.py` for all new registry behaviors:

```python
def test_list_toolchains_includes_disabled_rows(ai_service, toolchain):
    AiToolchainTable.create(
        toolchain_id="disabled-tc",
        display_name="Disabled",
        version="2",
        entrypoint="disabled.py",
        enabled=False,
    )

    result = ai_service.list_toolchains()

    assert [row["toolchain_id"] for row in result] == ["disabled-tc", "motor-ai"]


def test_save_toolchain_rename_rewrites_all_binding_rows_but_not_history(ai_service, config, toolchain):
    second = ConfigTable.create(config_name="Cfg2")
    original_created_time = toolchain.created_time
    first_binding = AiBindingTable.create(
        config=config,
        toolchain=toolchain,
        enabled=True,
        timeout_ms=1234,
        params_json='{"a": 1}',
        score_threshold=0.8,
    )
    second_binding = AiBindingTable.create(
        config=second,
        toolchain=toolchain,
        enabled=False,
        timeout_ms=4321,
        params_json='{"b": 2}',
        score_threshold=None,
    )
    AiExecutionTable.create(
        request_id="req-1",
        test_run_id="run-1",
        config_id=config.config_id,
        toolchain_id="motor-ai",
        module_status="success",
        is_current_effective=True,
    )

    ai_service.save_toolchain(
        "motor-ai",
        {
            "toolchain_id": "motor-ai-v2",
            "display_name": "Motor AI",
            "version": "2.0",
            "entrypoint": "python motor_ai_v2.py",
            "default_timeout_ms": 45000,
            "enabled": True,
            "description": "updated",
        },
    )

    renamed_toolchain = AiToolchainTable.get_by_id("motor-ai-v2")
    assert renamed_toolchain.created_time == original_created_time
    assert renamed_toolchain.updated_time
    rebound_first = AiBindingTable.get_by_id(first_binding.binding_id)
    rebound_second = AiBindingTable.get_by_id(second_binding.binding_id)
    assert rebound_first.binding_id == first_binding.binding_id
    assert rebound_first.config_id == config.config_id
    assert rebound_first.toolchain_id == "motor-ai-v2"
    assert rebound_first.timeout_ms == 1234
    assert rebound_first.params_json == '{"a": 1}'
    assert rebound_first.score_threshold == 0.8
    assert rebound_first.updated_time
    assert rebound_second.binding_id == second_binding.binding_id
    assert rebound_second.config_id == second.config_id
    assert rebound_second.toolchain_id == "motor-ai-v2"
    assert rebound_second.timeout_ms == 4321
    assert rebound_second.params_json == '{"b": 2}'
    assert rebound_second.score_threshold is None
    assert rebound_second.updated_time
    assert AiExecutionTable.get_by_id("req-1").toolchain_id == "motor-ai"
```

Also add failing tests for:

- `save_toolchain(None, data)` creates a new row and normalizes fixed fields to `protocol_version=1` and `execution_mode="oneshot_process"`
- create/update paths trim persisted string fields before writing
- update paths ignore caller-provided alternate `protocol_version` / `execution_mode` values and force the supported runtime values instead
- duplicate or empty `toolchain_id` raises `ValueError`
- empty `display_name` or `entrypoint` raises `ValueError`
- non-positive `default_timeout_ms` raises `ValueError`
- `delete_toolchain` clears affected bindings before deleting the row while preserving `binding_id`, `config_id`, `timeout_ms`, `params_json`, and `score_threshold`, updating `updated_time`, and leaving `ai_execution_table` rows untouched
- `list_toolchain_bindings("toolchain-id")` returns `config_id` and `config_name`
- rename rollback leaves both the original toolchain row and all binding FKs unchanged when the transactional update fails mid-operation
- delete rollback leaves both the toolchain row and all binding FKs unchanged when binding-clear or delete work fails mid-operation

- [ ] **Step 2: Run the targeted service tests to confirm they fail**

Run: `pytest tests/test_ai_service.py -k "toolchain or list_toolchains or save_toolchain or delete_toolchain" -v`

Expected: FAIL with missing `AiService` registry methods or assertion failures around rename/delete behavior.

- [ ] **Step 3: Implement the minimal registry CRUD surface in `AiService`**

Add explicit registry APIs instead of overloading the config-binding methods:

```python
def list_toolchains(self) -> list[dict]:
    rows = (
        AiToolchainTable.select()
        .order_by(AiToolchainTable.toolchain_id)
        .dicts()
    )
    return list(rows)


def list_toolchain_bindings(self, toolchain_id: str) -> list[dict]:
    rows = (
        TestConfigAiBindingTable.select(
            TestConfigAiBindingTable.config_id,
            TestConfigTable.config_name,
        )
        .join(TestConfigTable)
        .where(TestConfigAiBindingTable.toolchain_id == toolchain_id)
        .order_by(TestConfigAiBindingTable.config_id)
        .dicts()
    )
    return list(rows)
```

Implement one transactional save path with these rules:

- `save_toolchain(original_toolchain_id: str | None, data: dict)` handles create, update, and rename
- trim string fields before validation
- force `protocol_version = 1` and `execution_mode = "oneshot_process"`
- preserve `created_time` on rename
- rewrite all binding rows that still reference the old id
- preserve binding row payload fields other than the FK
- do not touch `AiExecutionTable`

Implement delete with these rules:

- `delete_toolchain(toolchain_id: str)` first clears affected binding rows by setting `enabled=False`, `toolchain_id=None`, and `updated_time=now_str()`
- then deletes the toolchain row
- leaves history rows untouched
- keep rename/delete work inside one transaction and verify rollback on injected mid-operation failure paths

- [ ] **Step 4: Run the targeted service tests to verify they pass**

Run: `pytest tests/test_ai_service.py -k "toolchain or list_toolchains or save_toolchain or delete_toolchain" -v`

Expected: PASS

- [ ] **Step 5: Commit the service slice**

Run:

```bash
git add tests/test_ai_service.py app/service/ai_service.py
git commit -m "feat: add ai toolchain registry service"
```

## Task 2: Add Registry-Change Signaling and Safe Config-Page Refresh

**Files:**
- Modify: `app/services_container.py`
- Modify: `ui/modules/config.py`
- Modify: `tests/test_config_module.py`
- Modify: `tests/test_service_container.py`

- [ ] **Step 1: Write the failing config-page refresh and stale-save tests**

Extend `tests/test_config_module.py` using the existing `_SignalStub` / `_AiServiceStub` style:

```python
def test_ai_toolchains_changed_refreshes_clean_binding_editor(qapp):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "tc-1",
            "timeout_ms": 1000,
            "params_json": '{"mode": "v1"}',
        }
    )
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-1", "display_name": "Toolchain 1", "version": "1"},
    ]
    module = _make_module(ai_service=ai_service, bus=bus)
    module._current_config_id = 1
    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-2",
        "timeout_ms": 2000,
        "params_json": '{"mode": "v2"}',
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-2", "display_name": "Toolchain 2", "version": "2"},
    ]

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-2"
    assert module.spin_ai_timeout.value() == 2000
    assert module.txt_ai_params.toPlainText() == '{"mode": "v2"}'


def test_save_rejects_dirty_stale_toolchain_selection(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr("ui.modules.config.QMessageBox.warning", lambda *args: warnings.append(args[1:3]))

    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    ai_service = _AiServiceStub()
    ai_service.enabled_toolchains = []
    module = _make_module(ai_service=ai_service, bus=bus)
    module._current_config_id = 1
    module.chk_ai_enabled.setChecked(True)
    module.combo_ai_toolchain.addItem("Ghost", "gone")
    module.combo_ai_toolchain.setCurrentIndex(module.combo_ai_toolchain.count() - 1)

    module._on_save()

    assert warnings == [("AI 配置错误", "AI 工具链不可用，请重新选择或清空绑定")]
```

Also add a failing test proving that `ai_toolchains_changed` does not silently overwrite unsaved local AI edits before save.

Add one more failing test to preserve the current unavailable-selection behavior:

- when a loaded config is already bound to a disabled or deleted toolchain, the combo still injects the bound id as an unavailable option instead of silently clearing the selection
- name that test with an `unavailable_toolchain` substring so it is covered by the targeted `-k` run below
- add a rename-specific dirty-edit test proving an unsaved old `toolchain_id` selection is not silently remapped to a renamed id after `ai_toolchains_changed`; it must stay local until save, then fail validation if stale

Add a direct event-bus contract test in `tests/test_service_container.py`:

```python
def test_event_bus_exposes_ai_toolchains_changed_signal():
    bus = EventBus()
    received = []
    bus.ai_toolchains_changed.connect(lambda: received.append("fired"))
    bus.ai_toolchains_changed.emit()
    assert received == ["fired"]
```

- [ ] **Step 2: Run the targeted config-module tests to confirm they fail**

Run: `pytest tests/test_config_module.py -k "ai_toolchains_changed or stale_toolchain or unavailable_toolchain" -v`

Expected: FAIL because the event bus has no `ai_toolchains_changed` signal and `ConfigModule` does not track or validate local AI-binding dirtiness.

Run: `pytest tests/test_service_container.py -k "ai_toolchains_changed" -v`

Expected: FAIL because `EventBus` does not yet define the new signal.

- [ ] **Step 3: Implement safe registry refresh behavior**

Add the signal in `EventBus`:

```python
class EventBus(QObject):
    ...
    config_changed = pyqtSignal()
    ai_toolchains_changed = pyqtSignal()
```

Update `ConfigModule` to:

- connect `self.services.bus.ai_toolchains_changed` when present
- track `_ai_binding_dirty`
- mark AI controls dirty on checkbox/combo/spin/text edits
- refresh options only when dirty state must be preserved
- fully reload the binding card only when the local AI editor is clean
- preserve the existing injected-unavailable-option behavior for already bound disabled/deleted toolchains
- validate selected `toolchain_id` at save time before calling `save_binding`

Use a dedicated validation helper so save-time behavior is explicit:

```python
def _validate_ai_binding_selection(self, ai_data: dict) -> None:
    if not ai_data["enabled"]:
        return
    tc_id = ai_data.get("toolchain_id")
    if tc_id is None:
        raise ValueError("请选择 AI 工具链")
    valid_ids = {tc["toolchain_id"] for tc in self.services.ai_service.list_enabled_toolchains()}
    if tc_id not in valid_ids:
        raise ValueError("AI 工具链不可用，请重新选择或清空绑定")
```

- [ ] **Step 4: Run the targeted config-module tests to verify they pass**

Run: `pytest tests/test_config_module.py -k "ai_toolchains_changed or stale_toolchain or unavailable_toolchain" -v`

Expected: PASS

Run: `pytest tests/test_service_container.py -k "ai_toolchains_changed" -v`

Expected: PASS

- [ ] **Step 5: Commit the signaling/config-refresh slice**

Run:

```bash
git add app/services_container.py ui/modules/config.py tests/test_config_module.py tests/test_service_container.py
git commit -m "feat: refresh config ai bindings on registry changes"
```

## Task 3: Build the `AI工具管理` Module CRUD UI

**Files:**
- Create: `ui/modules/ai_tool_management.py`
- Create: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Write the failing widget tests for empty state, selection, create, and save**

Create `tests/test_ai_tool_management_module.py` with a dedicated registry-service stub and Qt fixture usage matching existing widget tests:

```python
def test_module_shows_empty_state_when_no_toolchains_exist(qapp):
    module, service, _bus = _make_module(toolchains=[])

    assert module.empty_label.isVisible() is True
    assert module.detail.isVisible() is False


def test_save_new_toolchain_emits_registry_refresh(qapp):
    module, service, bus = _make_module(toolchains=[])

    module.btn_new.click()
    module.ed_toolchain_id.setText("motor-ai")
    module.ed_display_name.setText("Motor AI")
    module.ed_version.setText("1.0")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.spin_timeout.setValue(30000)
    module.chk_enabled.setChecked(True)
    module.txt_description.setPlainText("registry entry")

    module.btn_save.click()

    saved = service.saved_payloads[-1]
    assert saved["toolchain_id"] == "motor-ai"
    assert bus.ai_toolchains_changed.emissions == [((), {})]
```

Also add failing tests for:

- selecting a row populates the editor
- left-list row text includes at least `display_name` and `version`
- left-list ordering is by `toolchain_id`
- disabled rows remain visible in the left list
- disabled rows are visibly differentiated in the left list, for example with a `[禁用]` suffix in the item text
- the detail pane shows read-only helper text for `protocol_version = 1` and `execution_mode = oneshot_process`
- edit/save updates the existing row instead of creating a duplicate
- editing an existing row’s `enabled` checkbox persists the new enabled/disabled state and emits `ai_toolchains_changed`
- delete button is disabled when nothing is selected
- duplicate `toolchain_id`, empty required fields, and invalid `default_timeout_ms` surface user-visible validation errors from the widget before any service write

- [ ] **Step 2: Run the new widget tests to confirm they fail**

Run: `pytest tests/test_ai_tool_management_module.py -v`

Expected: FAIL because the module file does not exist yet.

- [ ] **Step 3: Implement the new master/detail widget**

Create `ui/modules/ai_tool_management.py` with the same overall structure as `ConfigModule`:

```python
class AiToolManagementModule(QWidget):
    module_id = "ai_tool_management"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._current_toolchain_id = None
        self._editor_snapshot = None
        self._is_new_draft = False
        self._init_ui()
        self._connect_signals()
        self._refresh_list()
```

Implement:

- left `QListWidget` + `新建` / `删除`
- populate left-list row labels from `display_name` and `version`, and keep the visible list sorted by `toolchain_id`
- visibly differentiate disabled rows in the left list, for example by appending `[禁用]`
- right detail editor with:
  - `toolchain_id`
  - `display_name`
  - `version`
  - `entrypoint`
  - `default_timeout_ms`
  - `enabled`
  - `description`
- read-only helper label for fixed runtime fields
- empty state when the registry list is empty
- save path calling `self.services.ai_service.save_toolchain(...)`
- validation path surfacing `QMessageBox.warning(...)` for duplicate/empty/invalid field failures raised locally or by `AiService`
- success path refreshing the list, reselecting the saved row, and emitting `bus.ai_toolchains_changed`

- [ ] **Step 4: Run the new widget tests to verify they pass**

Run: `pytest tests/test_ai_tool_management_module.py -v`

Expected: PASS

- [ ] **Step 5: Commit the new module slice**

Run:

```bash
git add ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git commit -m "feat: add ai tool management module"
```

## Task 4: Add Dirty-State Prompts and Delete Confirmation to the New Module

**Files:**
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Write the failing dirty-state and delete tests**

Extend `tests/test_ai_tool_management_module.py` with explicit prompt-order coverage:

```python
def test_delete_dirty_row_prompts_unsaved_changes_before_delete_confirmation(qapp, monkeypatch):
    module, service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    module._select_toolchain("motor-ai")
    module.ed_display_name.setText("Edited Name")

    flow = []
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: flow.append("dirty") or "discard")
    monkeypatch.setattr(module, "_confirm_delete", lambda *args, **kwargs: flow.append("delete") or False)

    module.btn_delete.click()

    assert flow == ["dirty", "delete"]


def test_row_switch_cancel_keeps_current_dirty_editor(qapp, monkeypatch):
    module, service, _bus = _make_module(toolchains=[_toolchain("a"), _toolchain("b")])
    module._select_toolchain("a")
    module.ed_display_name.setText("Unsaved")
    monkeypatch.setattr(module, "_prompt_unsaved_changes", lambda action: "cancel")

    module._on_item_changed(_item_for(module, "b"), _item_for(module, "a"))

    assert module._current_toolchain_id == "a"
    assert module.ed_display_name.text() == "Unsaved"
```

Also add failing tests for:

- `新建` while dirty
- tab/window leave helper returning cancel
- delete confirmation dialog includes the selected toolchain identity, affected names/count, and an explicit warning that those configs will become unbound
- delete clearing bindings and emitting `ai_toolchains_changed`
- editing an existing row to toggle `enabled` from true to false (and back) persists correctly and emits `ai_toolchains_changed`
- choosing save during an unsaved-changes prompt but hitting a save validation failure keeps the current editor visible and blocks the pending row-switch/delete/tab-leave action

- [ ] **Step 2: Run the dirty-state/delete tests to confirm they fail**

Run: `pytest tests/test_ai_tool_management_module.py -k "dirty or delete or cancel" -v`

Expected: FAIL because the module does not yet implement unsaved-change mediation or binding-aware delete confirmation.

- [ ] **Step 3: Implement the dirty-state mediator and delete flow**

Add one reusable decision helper instead of scattering prompt logic:

```python
def _resolve_pending_editor_action(self, action: str) -> bool:
    if not self._is_dirty():
        return True

    decision = self._prompt_unsaved_changes(action)
    if decision == "cancel":
        return False
    if decision == "discard":
        self._restore_snapshot()
        return True
    if decision == "save":
        return self._save_current_editor()
    return False
```

Delete flow requirements in code:

- expose a public `confirm_leave()` method that reuses the same unsaved-changes decision contract for tab switches and window close
- route row selection and `新建` through the same dirty-state helper instead of separate one-off logic
- call `_resolve_pending_editor_action("delete")` first
- fetch affected bindings through `self.services.ai_service.list_toolchain_bindings(toolchain_id)`
- render confirmation text with the selected toolchain identity, names, count, and explicit unbind warning
- only call `delete_toolchain` after explicit confirmation
- if `_save_current_editor()` fails during a pending action, keep the current editor state visible and abort the pending action

- [ ] **Step 4: Run the dirty-state/delete tests to verify they pass**

Run: `pytest tests/test_ai_tool_management_module.py -k "dirty or delete or cancel" -v`

Expected: PASS

- [ ] **Step 5: Commit the dirty-state/delete slice**

Run:

```bash
git add ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git commit -m "feat: add ai tool management dirty state flow"
```

## Task 5: Integrate the New Page Into `MainWindow`

**Files:**
- Modify: `app/main_window.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing main-window navigation/permission tests**

Extend `tests/test_main_window.py` to cover the new tab and role access:

```python
def test_main_window_includes_ai_tool_management_tab_in_topbar(qapp, monkeypatch):
    _patch_non_target_modules(monkeypatch)
    monkeypatch.setattr(main_window_mod, "AiToolManagementModule", _StubModule, raising=False)
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    assert window.tab_names == ["测试", "数据管理", "配置", "AI工具管理", "硬件"]


def test_engineer_role_can_access_ai_tool_management_tab(qapp, monkeypatch):
    _patch_non_target_modules(monkeypatch)
    monkeypatch.setattr(main_window_mod, "AiToolManagementModule", _StubModule, raising=False)
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "工程师"
    window.set_role()

    assert window.top_button_dict["AI工具管理"].isEnabled() is True
```

Also add a failing operator-role test proving `操作员` cannot access the new tab.

Add an explicit `管理员` role test proving the new tab is enabled there too.

Add two failing guard-flow tests that use a stub `AiToolManagementModule` with a `confirm_leave()` method:

- switching away from the `AI工具管理` tab is blocked when `confirm_leave()` returns `False`
- closing the main window is blocked when `confirm_leave()` returns `False`

- [ ] **Step 2: Run the targeted main-window tests to confirm they fail**

Run: `pytest tests/test_main_window.py -k "AI工具管理 or ai_tool_management" -v`

Expected: FAIL because the top-tab list, page builder, and role map do not yet include the new module.

- [ ] **Step 3: Implement the main-window wiring**

Update `app/main_window.py` to:

- import `AiToolManagementModule`
- insert `build_ai_tool_management_page()` after `build_config_page()`
- extend `self.tab_names` to include `AI工具管理`
- add role-permission entries for `管理员` and `工程师`
- replace the direct `group.idClicked.connect(self.tab.setCurrentIndex)` wiring with an `_on_topbar_tab_clicked(index)` handler that can block a tab change when the current `AiToolManagementModule.confirm_leave()` call returns `False`
- on main-tab changes away from `AI工具管理`, consult the module's leave guard before finalizing the tab switch
- in `closeEvent`, consult the module's leave guard before closing the window

Use the same card-wrapper style as existing single-module pages:

```python
def build_ai_tool_management_page(self) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    self.ai_tool_management_module = AiToolManagementModule(self.services)
    lay.addWidget(Card("AI工具管理", self.ai_tool_management_module), 1)
    return page
```

Also update any local test helper that stubs module imports so `AiToolManagementModule` is patched like the other non-target modules.

- [ ] **Step 4: Run the targeted main-window tests to verify they pass**

Run: `pytest tests/test_main_window.py -k "AI工具管理 or ai_tool_management" -v`

Expected: PASS

- [ ] **Step 5: Commit the window-integration slice**

Run:

```bash
git add app/main_window.py tests/test_main_window.py
git commit -m "feat: add ai tool management tab"
```

## Task 6: Final Verification and Cleanup

**Files:**
- Modify: `tests/test_ai_service.py`
- Modify: `tests/test_config_module.py`
- Modify: `tests/test_ai_tool_management_module.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_service_container.py`
- Modify: `app/service/ai_service.py`
- Modify: `app/services_container.py`
- Modify: `ui/modules/config.py`
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `app/main_window.py`

- [ ] **Step 1: Run the focused regression set for the full feature**

Run:

```bash
pytest tests/test_ai_service.py tests/test_config_module.py tests/test_ai_tool_management_module.py tests/test_main_window.py tests/test_service_container.py -v
```

Expected: PASS

- [ ] **Step 2: Run the full test suite**

Run: `pytest -v`

Expected: PASS

- [ ] **Step 3: Inspect the final diff for scope control**

Run:

```bash
git status --short
git diff -- app/service/ai_service.py app/services_container.py ui/modules/config.py ui/modules/ai_tool_management.py app/main_window.py tests/test_ai_service.py tests/test_config_module.py tests/test_ai_tool_management_module.py tests/test_main_window.py tests/test_service_container.py
```

Expected: Only the planned files above are changed, with no unrelated edits or accidental refactors.

- [ ] **Step 4: Commit the final verification pass**

Run:

```bash
git add app/service/ai_service.py app/services_container.py ui/modules/config.py ui/modules/ai_tool_management.py app/main_window.py tests/test_ai_service.py tests/test_config_module.py tests/test_ai_tool_management_module.py tests/test_main_window.py
git add tests/test_service_container.py
git commit -m "test: verify ai tool management integration"
```
