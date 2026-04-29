# Workorder Phase Summary Only Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the trigger-summary line from the workorder card so only `当前阶段` remains between the config list and the control buttons.

**Architecture:** Keep the change local to the workorder UI module and its focused tests. Remove the trigger-summary widget creation and update path, then tighten tests so they prove the extra summary row is gone while preserving current-phase rendering.

**Tech Stack:** Python, PyQt6, pytest

---

### Task 1: Remove Trigger Summary UI From Workorder Module

**Files:**
- Modify: `ui/modules/workorder.py`
- Test: `tests/test_workorder_module.py`

- [ ] **Step 1: Write the failing test**

Add or update focused assertions so the module verifies:

```python
module, services = _make_module()
services.bus.factory_state_changed.emit(
    FactoryControllerState(
        selected_config_ids=(101,),
        run_phase="armed",
        trigger_source="manual",
    )
)

assert hasattr(module, "lbl_trigger_summary") is False
assert "最近触发方式" not in _workorder_rendered_text(module)
assert module.lbl_phase_summary.text() == "当前阶段：待启动"
```

Also update the test helper and trigger-summary-specific coverage in `tests/test_workorder_module.py` so they reflect the new requirement:

- remove `lbl_trigger_summary` reads from `_workorder_rendered_text()`
- replace the dedicated trigger-summary rendering test with assertions that the trigger-summary widget/row is absent
- keep existing phase-summary coverage intact

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workorder_module.py -v`
Expected: FAIL because the current module still exposes and renders trigger-summary UI while the updated tests require it to be absent.

- [ ] **Step 3: Write minimal implementation**

Update `ui/modules/workorder.py` to:

- remove `lbl_trigger_summary` construction
- stop adding it to the layout
- simplify `_update_phase_trigger_summary()` so it only updates `lbl_phase_summary`
- remove any now-unused trigger display helper if it becomes dead code

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workorder_module.py -v`
Expected: PASS with phase-summary assertions still green and no trigger-summary UI remaining.

- [ ] **Step 5: Commit**

```bash
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "refactor(workorder): remove trigger summary row"
```
