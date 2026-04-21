# AI Tool Management Layout Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the clipped `浏览` button and oversized `默认超时` row on the `AI工具管理` page without changing saved values or service behavior.

**Architecture:** Keep this as a UI-only patch in `AiToolManagementModule` plus targeted widget tests. The package-directory row and timeout row should expose inspectable row widgets, compute a shared single-line height from current size hints, and use fixed-height vertical policies so the form cannot stretch those rows unexpectedly.

**Tech Stack:** Python, PyQt6, pytest, git, PowerShell.

---

## Execution Root

Execute this plan from a fresh dedicated worktree branched from the current approved base commit, not from the dirty main checkout.

Example worktree root:

```text
D:\Work\jingchengpython\JingChengMechanical\.worktrees\ai-tool-management-layout-followup
```

All verification, diff checks, and review ranges in this plan assume that clean dedicated worktree.

## File Map

- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

## Task 1: Fix AI Tool Management Row Heights

**Files:**
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Create or switch to the clean dedicated worktree**

Run from the main repo root:

```powershell
git worktree add .worktrees/ai-tool-management-layout-followup -b ai-tool-management-layout-followup 46af880
```

Then change working directory to:

```text
D:\Work\jingchengpython\JingChengMechanical\.worktrees\ai-tool-management-layout-followup
```

Expected: the clean dedicated worktree is created from approved base commit `46af880`, and all later commands in this task run from that worktree.

- [ ] **Step 2: Record the task baseline**

Run:

```powershell
$BASE_SHA = git rev-parse HEAD
$BASE_SHA
```

Expected: save the printed SHA as `$BASE_SHA` for later diff and review-range commands.

- [ ] **Step 3: Write failing tests for the follow-up regressions**

Update `tests/test_ai_tool_management_module.py` with assertions that fail against the current layout:

```python
def test_package_dir_row_uses_shared_fixed_single_line_height(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    assert module.package_dir_row.sizePolicy().verticalPolicy().name == "Fixed"
    assert module.btn_browse_package_dir.height() >= module.btn_browse_package_dir.sizeHint().height()
    assert module.ed_package_dir.height() == module.btn_browse_package_dir.height()
    assert module.package_dir_row.height() == module.ed_package_dir.height()
```

Also add or update assertions proving:

- `module.timeout_row` exists
- `module.timeout_row.sizePolicy().verticalPolicy().name == "Fixed"`
- `module.timeout_row.height() >= module.spin_timeout.sizeHint().height()`
- `module.timeout_row.layout().indexOf(module.spin_timeout) >= 0`
- `module.timeout_row.layout().indexOf(module.lb_timeout_unit) >= 0`
- `module.lb_timeout_unit.text() == "ms"` and `module.lb_timeout_unit.isVisible() is True`
- `module.spin_timeout.text()` still contains only the integer value

- [ ] **Step 4: Preserve existing behavior tests**

Keep these tests passing and do not weaken their assertions:

- `test_large_timeout_value_loads_and_saves_without_clamping`
- `test_save_persists_selected_execution_mode`

They protect the approved “UI-only, no data-behavior change” boundary.

- [ ] **Step 5: Run the UI test file to verify failure**

Run:

```powershell
pytest tests/test_ai_tool_management_module.py -q
```

Expected: FAIL before implementation because the current browse button height is too small and the timeout row does not use a fixed single-line container.

- [ ] **Step 6: Implement the layout-only patch**

In `ui/modules/ai_tool_management.py`:

- keep the current `self.package_dir_row` API
- compute `package_dir_height = max(self.ed_package_dir.sizeHint().height(), self.btn_browse_package_dir.sizeHint().height())`
- apply that computed height to `self.ed_package_dir`, `self.btn_browse_package_dir`, and `self.package_dir_row`
- keep `self.package_dir_row` vertical size policy as `QSizePolicy.Policy.Fixed`
- call `self.package_dir_row.setFixedHeight(package_dir_height)`
- expose the timeout row as `self.timeout_row`
- compute `timeout_row_height = max(self.spin_timeout.sizeHint().height(), self.lb_timeout_unit.sizeHint().height())`
- set `self.timeout_row` vertical size policy to `QSizePolicy.Policy.Fixed`
- set `self.timeout_row` fixed height to `timeout_row_height`
- keep both `self.spin_timeout` and `self.lb_timeout_unit` inside `self.timeout_row`
- keep `self.spin_timeout` and `self.lb_timeout_unit` vertically aligned inside `self.timeout_row`
- keep the external `ms` label and current timeout editor semantics unchanged
- do not change `_collect_payload()`, `_collect_save_payload()`, `_validate_payload()`, `AiService`, database code, runtime code, or `.dy` code

- [ ] **Step 7: Run focused tests**

Run:

```powershell
pytest tests/test_ai_tool_management_module.py -q
```

Expected: PASS.

- [ ] **Step 8: Run adjacent tests**

Run:

```powershell
pytest tests/test_ai_service.py tests/test_ai_tool_management_module.py -q
```

Expected: PASS.

- [ ] **Step 9: Report changed files for orchestrator review**

Run:

```powershell
git diff --name-only $BASE_SHA -- ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git diff --name-only $BASE_SHA
```

Expected:

- the file-scoped diff lists:
  - `ui/modules/ai_tool_management.py`
  - `tests/test_ai_tool_management_module.py`
- because this task runs in a fresh dedicated worktree, the full diff from `$BASE_SHA` also contains no service, database, runtime, or `.dy` files

- [ ] **Step 10: Paired review gate**

After the orchestrator has the implementation diff range, dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for that range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Final Review

- [ ] Confirm the implementation diff only touches `ui/modules/ai_tool_management.py` and `tests/test_ai_tool_management_module.py`.
- [ ] Confirm all checks were run from the clean dedicated worktree, not the dirty main checkout.
- [ ] Confirm the required and adjacent verification commands pass.
- [ ] Use `verification-before-completion` before reporting completion.
