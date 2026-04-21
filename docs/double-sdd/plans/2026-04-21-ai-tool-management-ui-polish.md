# AI Tool Management UI Polish Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the `AI工具管理` detail form so package directory alignment, timeout units, and helper-text visibility match the approved UI behavior.

**Architecture:** Keep this as a UI-only adjustment in `AiToolManagementModule`. Tests verify widget structure, alignment hints, timeout unit display, large timeout preservation, and removal of runtime helper copy. No service, database, runtime, or `.dy` packaging logic changes are allowed.

**Tech Stack:** Python, PyQt6, pytest, git, PowerShell.

---

## File Map

- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

## Task 1: Polish AI Tool Management Form Layout

**Files:**
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Record the task baseline**

Run:

```powershell
$BASE_SHA = git rev-parse HEAD
$BASE_SHA
```

Expected: save the printed SHA as `$BASE_SHA` for every later diff and review-range command in this task.

- [ ] **Step 2: Write failing UI structure tests**

Update `tests/test_ai_tool_management_module.py` with focused assertions that fail against the current layout:

```python
def test_package_dir_row_is_bounded_and_aligned(qapp):
    module, _service, _bus = _make_module(toolchains=[_toolchain("motor-ai")])
    _show(module, qapp)

    layout = module.package_dir_row.layout()
    editor_index = layout.indexOf(module.ed_package_dir)
    button_index = layout.indexOf(module.btn_browse_package_dir)
    assert editor_index >= 0
    assert button_index >= 0
    expected_alignment = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignTop
    assert layout.itemAt(editor_index).alignment() & expected_alignment
    assert layout.itemAt(button_index).alignment() & expected_alignment
    assert module.package_dir_row.sizePolicy().verticalPolicy().name in {
        "Fixed",
        "Maximum",
    }
    assert module.btn_browse_package_dir.height() == module.ed_package_dir.height()
```

Also add or update assertions proving:

- `module.spin_timeout.alignment()` is `Qt.AlignmentFlag.AlignLeft`
- a visible `module.lb_timeout_unit` exists with text `ms`
- the timeout editor text contains only the integer value, for example `module.spin_timeout.text() == "45000"` after loading a `45000` ms row
- the timeout editor no longer uses the old in-editor suffix/placeholder for `ms`, for example `module.spin_timeout.placeholderText() == ""`
- runtime helper copy is gone by checking `not hasattr(module, "runtime_helper")`
- no `QLabel` child contains `运行时说明`, `protocol_version`, `resident_process`, or `oneshot_process`

- [ ] **Step 3: Preserve existing non-visual behavior coverage**

Keep `test_large_timeout_value_loads_and_saves_without_clamping` passing. If the timeout row is refactored into a wrapper, the editor must remain `_LargeIntegerEditor`, not a clamped `QSpinBox`.

Keep `test_save_persists_selected_execution_mode` passing and do not weaken its assertions. It must continue proving that `execution_mode` and `package_dir` are collected and saved exactly as before this UI-only refactor.

- [ ] **Step 4: Run tests to verify failure**

Run:

```powershell
pytest tests/test_ai_tool_management_module.py -q
```

Expected: FAIL before implementation because the current timeout editor is right-aligned, no fixed `ms` label exists, and `runtime_helper` still exists.

- [ ] **Step 5: Implement the UI polish**

In `ui/modules/ai_tool_management.py`:

- make `_LargeIntegerEditor` left-align text
- keep `_LargeIntegerEditor` large-integer behavior unchanged
- expose the package directory row widget as `self.package_dir_row`
- add a timeout row widget containing `self.spin_timeout` and `self.lb_timeout_unit = QLabel("ms")`
- add the timeout row to the form with label `默认超时`
- set the timeout row layout margins to zero and align contents vertically
- do not call `self.spin_timeout.setSuffix(" ms")`; the unit is represented only by `self.lb_timeout_unit`
- adjust `self.package_dir_row` / its layout so the path editor and browse button are vertically aligned in a bounded single-line row
- add the package directory editor and browse button to the row layout with per-widget `AlignVCenter` or `AlignTop` alignment
- make the package directory row vertical size policy fixed or maximum, and make the browse button height match the package directory editor height
- remove `self.runtime_helper` and the `运行时说明` form row
- do not change `_collect_payload()`, `_collect_save_payload()`, `_validate_payload()`, `AiService`, database code, runtime code, or `.dy` code

- [ ] **Step 6: Run focused tests**

Run:

```powershell
pytest tests/test_ai_tool_management_module.py -q
```

Expected: PASS.

- [ ] **Step 7: Run adjacent tests**

Run:

```powershell
pytest tests/test_ai_service.py tests/test_ai_tool_management_module.py -q
```

Expected: PASS.

- [ ] **Step 8: Report changed files for orchestrator review**

Run:

```powershell
git rev-parse HEAD
git diff --name-only $BASE_SHA -- ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git diff --name-only $BASE_SHA
```

Expected:

- `git rev-parse HEAD` prints the current task head SHA if the implementer committed, or the unchanged base SHA if the work is still uncommitted
- the file-scoped diff lists these implementation files:
  - `ui/modules/ai_tool_management.py`
  - `tests/test_ai_tool_management_module.py`
- the full diff from `<BASE_SHA>` contains no production/service/database/runtime/`.dy` files; if docs were already changed before implementation, report them separately as orchestration artifacts

The orchestrator will create a review commit or otherwise produce the final review range after independently verifying the diff.

- [ ] **Step 9: Paired review gate**

After the orchestrator has a concrete git range for the implementation diff, dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for that range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Final Review

- [ ] Confirm the implementation diff only touches `ui/modules/ai_tool_management.py` and `tests/test_ai_tool_management_module.py`.
- [ ] Confirm the review range is based on the task baseline SHA, not the dirty main checkout state.
- [ ] Confirm the required and adjacent verification commands pass.
- [ ] Use `verification-before-completion` before reporting completion.
