# PLC Settings Test Module Removal Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the non-owned PLC settings test module from this branch without changing PLC production code or current `.dy` import/export behavior.

**Architecture:** This is a single-file test deletion. The implementation removes `tests/test_plc_settings_module.py`, then verifies the file is gone and re-runs the current feature-boundary regression commands that do not depend on that file. No production modules, pytest configuration, or replacement tests are introduced.

**Tech Stack:** Python, pytest, git, PowerShell.

---

## Execution Root

Run every command in this plan from this exact worktree:

```text
D:\Work\jingchengpython\JingChengMechanical\.worktrees\config-dy-import-export
```

Do not execute any step from the main repo checkout at `D:\Work\jingchengpython\JingChengMechanical`.

## File Map

- Delete: `tests/test_plc_settings_module.py`

### Task 1: Delete The PLC Settings Test Module

**Files:**
- Delete: `tests/test_plc_settings_module.py`

- [ ] **Step 1: Record the base commit and confirm the target file exists before deletion**

Run:

```powershell
git rev-parse HEAD
Test-Path tests/test_plc_settings_module.py
```

Expected:

- `git rev-parse HEAD` prints the base SHA for this task review range
- `Test-Path` returns `True`

- [ ] **Step 2: Delete the test file**

Remove exactly this file:

```text
tests/test_plc_settings_module.py
```

Do not modify any PLC production code, hardware management code, pytest config, or unrelated tests.

- [ ] **Step 3: Verify the file is gone**

Run:

```powershell
Test-Path tests/test_plc_settings_module.py
```

Expected: `False`

- [ ] **Step 4: Run focused regression command 1**

Run:

```powershell
pytest tests/test_config_service.py tests/test_config_module.py tests/test_ai_service.py tests/test_ai_tool_management_module.py tests/test_golden_sample_service.py tests/test_config_package_service.py -q
```

Expected: PASS

- [ ] **Step 5: Run focused regression command 2**

Run:

```powershell
pytest tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_db_connection_service.py tests/test_service_container.py -q
```

Expected: PASS

- [ ] **Step 6: Commit the deletion**

Run:

```powershell
git add -u tests/test_plc_settings_module.py
git commit -m "test: remove plc settings module tests"
```

- [ ] **Step 7: Paired review gate**

After Step 6, capture the new head SHA:

```powershell
git rev-parse HEAD
```

Then dispatch:

```text
spec-code-reviewer
Description: Delete tests/test_plc_settings_module.py only
Spec: docs/double-sdd/specs/2026-04-21-plc-settings-test-removal-design.md
Plan: docs/double-sdd/plans/2026-04-21-plc-settings-test-removal.md
Base: <Step 1 SHA>
Head: <Step 6 SHA>
```

```text
quality-code-reviewer
Description: Delete tests/test_plc_settings_module.py only
Spec: docs/double-sdd/specs/2026-04-21-plc-settings-test-removal-design.md
Plan: docs/double-sdd/plans/2026-04-21-plc-settings-test-removal.md
Base: <Step 1 SHA>
Head: <Step 6 SHA>
```

Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Final Review

- [ ] Confirm `tests/test_plc_settings_module.py` is deleted and no PLC production files changed.

Run:

```powershell
Test-Path tests/test_plc_settings_module.py
git diff --name-only <Step 1 SHA>..HEAD
```

Expected:

- `Test-Path` returns `False`
- `git diff --name-only <Step 1 SHA>..HEAD` lists only `tests/test_plc_settings_module.py`

- [ ] Use `verification-before-completion` before reporting completion.
