# Control Panel Status Row Removal Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the inline status row from the control panel, tighten the vertical layout slightly, and increase button minimum height so the two-row button stack remains visually balanced.

**Architecture:** Keep the change local to the control module and its directly affected tests. Remove the inline status-label widget and text updates, set fixed layout spacing and button minimum heights in the module, and update focused control/main-window tests so they verify the status row is gone while button-state and blocked-modal behavior remain intact.

**Tech Stack:** Python, PyQt6, pytest

---

### Task 1: Remove Control Panel Inline Status Row And Adjust Button Layout

**Files:**
- Modify: `ui/modules/control.py`
- Modify: `tests/test_control_module.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing tests**

Update the focused tests so they verify:

```python
module, _ = _make_module()

assert hasattr(module, "lbl_start_reason") is False
assert module.layout().spacing() == 8
assert module.btn_start.minimumHeight() == 36
assert module.btn_stop.minimumHeight() == 36
assert module.btn_pass.minimumHeight() == 36
assert module.btn_fail.minimumHeight() == 36
```

Also update the existing busy-state and main-window tests to replace any `lbl_start_reason` text assertions with the new requirement:

- the start button remains disabled in busy state
- `lbl_start_reason` is absent
- blocked-start modal behavior still works
- blocked-start modal tests no longer expect inline label text updates after state changes

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_control_module.py -v`
Run: `pytest tests/test_main_window.py -v`
Expected: FAIL because the current control module still creates and updates `lbl_start_reason`, uses the old spacing, and does not set the new button minimum heights.

- [ ] **Step 3: Write minimal implementation**

Update `ui/modules/control.py` to:

- remove `lbl_start_reason` construction and layout insertion
- stop inline status-text updates while preserving start-button enabled/disabled logic
- set the top-level control layout spacing to `8`
- set `setMinimumHeight(36)` on `btn_start`, `btn_stop`, `btn_pass`, and `btn_fail`

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_control_module.py -v`
Run: `pytest tests/test_main_window.py -v`
Expected: PASS with no inline status row, preserved button/modal behavior, and the new layout assertions green.

Then confirm the UI structure directly from the widget tree in the updated tests by asserting:

- `hasattr(module, "lbl_start_reason") is False`
- `module.layout().count() == 2`

This is the required evidence that the control panel now renders only the two button rows and no inline status label.

- [ ] **Step 5: Commit**

```bash
git add ui/modules/control.py tests/test_control_module.py tests/test_main_window.py
git commit -m "refactor(control): remove inline status row"
```
