# Main Window STFT Review Test Repair Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the pre-existing `tests/test_main_window.py` review/reset failures by making the shared review-ready test helper seed valid STFT render data before it asserts STFT selection-box visibility.

**Architecture:** Keep production code unchanged. Update the main-window test helper path so `_emit_review_ready(...)` emits a minimal STFT timeline for each requested review channel using the same `run_id`, then verify the two failing reset scenarios, the full `tests/test_main_window.py` file, and the full suite all pass.

**Tech Stack:** Python 3.12, pytest, PyQt6, NumPy

---

**Execution Root:** `D:/Work/jingchengpython/JingChengMechanical/.worktrees/test-suite-file-splitting-20260330`

## File Structure

- Modify: `tests/test_main_window.py`

## Task 1: Repair The Shared Review-Ready Test Helper

**Files:**
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Add a dedicated STFT test helper**

Implementation notes:

- add a helper in `tests/test_main_window.py` that emits STFT frames for every channel in the requested `channels` tuple
- emit frames at `0.0`, `1.0`, `2.0`, and `3.0` seconds for each channel using the existing `_make_stft_frame(...)` helper
- use the same `config_id` and `run_id` that the review payload will use
- keep the helper local to `tests/test_main_window.py`

- [ ] **Step 2: Route `_emit_review_ready(...)` through the STFT helper**

Implementation notes:

- update `_emit_review_ready(...)` so it emits the new STFT frames before it emits review state and the committed review payload
- preserve the existing `channels` argument contract so committed chart payloads and STFT cache seeding stay aligned
- do not change production code or weaken the reset assertions

- [ ] **Step 3: Run the two previously failing scenarios**

Run:

```powershell
Set-Location D:/Work/jingchengpython/JingChengMechanical/.worktrees/test-suite-file-splitting-20260330
pytest tests/test_main_window.py -k "next_run_reset_clears_result_stft_and_chart_cards_before_next_review_payload_renders or selection_changed_reset_clears_result_stft_and_chart_cards" -q
```

Expected: PASS

- [ ] **Step 4: Run the full main-window test file**

Run:

```powershell
Set-Location D:/Work/jingchengpython/JingChengMechanical/.worktrees/test-suite-file-splitting-20260330
pytest tests/test_main_window.py -q
```

Expected: PASS

- [ ] **Step 5: Run the full suite**

Run:

```powershell
Set-Location D:/Work/jingchengpython/JingChengMechanical/.worktrees/test-suite-file-splitting-20260330
pytest tests -q
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd D:/Work/jingchengpython/JingChengMechanical/.worktrees/test-suite-file-splitting-20260330
git add tests/test_main_window.py
git commit -m "test: seed stft data in main window review helper"
```
