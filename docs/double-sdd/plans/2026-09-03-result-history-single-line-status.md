# Result History Single-Line Status Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render result-history NG conclusions as a single-line `不通过：故障类型` status that stays at 32 px for a normal four-character fault and shrinks only when necessary.

**Architecture:** Replace the height-for-width wrapped status label with a width-fitted single-line label that owns the complete source text, semantic color, adaptive font calculation, and elided fallback. Keep the existing status update entry points and AI summary-table wrapping behavior, but canonicalize NG fault formatting at the card boundary.

**Tech Stack:** Python 3, PyQt6 widgets/font metrics, pytest, offscreen Qt tests

---

## File Structure

- Modify `ui/widgets/result_history_card.py`: canonical colon formatting and a single-line adaptive status label.
- Modify `tests/test_result_history_card.py`: replace wrapping expectations with single-line, default-size, shrink, elision, update, and preview coverage.
- Verify `tests/test_result_history_module.py`, `tests/test_result_dashboard_module.py`, and `tests/multi_run_integration/test_two_stage_analysis_projection.py` without changing projection payload expectations. Parenthesized upstream `summary_payload["overall_label"]` values are protected contract behavior; colon canonicalization occurs only in `ResultHistoryCard`.

### Task 1: Add Single-Line Status Regression Tests

**Files:**
- Modify: `tests/test_result_history_card.py`
- Test: `tests/test_result_history_card.py`

- [ ] **Step 1: Replace obsolete wrapped-status expectations**

Update the current large-status test and its expected NG strings from `不通过（故障类型）` to `不通过：故障类型`. Remove assertions requiring a multi-line wrapped height and replace them with assertions that the label has `wordWrap() is False`, uses a fixed 48 px status height, and retains centered, plain-text, accessible behavior.

- [ ] **Step 2: Add the four-character default-size regression**

Show a 360 px terminal NG card with fault type `风扇异常`, process layout events, and verify:

```python
assert overall.text() == "不通过：风扇异常"
assert overall.toolTip() == "结果摘要：不通过：风扇异常"
assert overall.wordWrap() is False
assert overall.height() == 48
assert "font-size: 32px" in overall.styleSheet()
```

Also assert the complete status is present in the label's accessible description.

- [ ] **Step 3: Add adaptive shrink and elision regressions**

Use a fault type long enough to exceed the 32 px width but short enough to fit at a reduced size. Verify the status remains one line, its height/card geometry does not grow, the visible text remains complete, and the parsed `font-size` is between 18 and 31 px.

Use an allowed 128-byte fault type that cannot fit even at 18 px. Verify the label style stops at `font-size: 18px`, the visible text is tail-elided, and both tooltip and accessible description retain the complete unelided `不通过：...` value.

- [ ] **Step 4: Cover update and manual-preview formatting**

Update a reusable card from a short result to a long NG result and verify font fitting is recalculated without changing status-label identity or height. Update `preview_manual_judgment("NG", fault_type)` expectations to the colon format, then preview `OK` and verify the label returns to `通过` at 32 px.

- [ ] **Step 5: Cover width-change refitting in both directions**

Show one existing card with a complete status that fits at 32 px at the production width. Narrow the card enough to require font reduction or the 18 px elision fallback, process queued events, and verify the same label refits without growing vertically. Widen the card back to 360 px and verify the complete visible text and 32 px style are restored.

- [ ] **Step 6: Run targeted tests and verify RED**

Run with `QT_QPA_PLATFORM=offscreen` using the repository virtual environment and the established pytest import order:

```powershell
& '.\.venv\Scripts\python.exe' -c "import sys; sys.path.append(r'D:\Program Files\Python\Python312\Lib\site-packages'); import pytest, PyQt6.QtCore; raise SystemExit(pytest.main(['tests/test_result_history_card.py', '-k', 'single_line or adaptive_status or manual_preview', '-v']))"
```

Expected: FAIL because NG still uses parentheses, the label wraps, and it has no width-based font fitting or elision.

### Task 2: Implement Colon Formatting and Width-Fitted Typography

**Files:**
- Modify: `ui/widgets/result_history_card.py`
- Test: `tests/test_result_history_card.py`

- [ ] **Step 1: Canonicalize overall NG text**

In `_overall_text`, when the frozen conclusion is NG and a fault type exists, return the canonical card-boundary value directly:

```python
return f"{_CONCLUSION_TEXT[ResultConclusion.NG]}：{self._snapshot.fault_type}"
```

Use the same `不通过：{fault_type}` construction in `preview_manual_judgment`. Leave bare NG as `不通过`.

- [ ] **Step 2: Replace the wrapped label with a fitted single-line label**

Replace `_WrappedStatusLabel` with an internal `_FittedStatusLabel` that stores the complete status and color, owns a single-shot zero-delay fit timer, refits when its width changes, and exposes a method such as `set_display(text, color)`.

The label must be configured with `wordWrap(False)`, a fixed 48 px height, centered alignment, and an expanding-horizontal/fixed-vertical size policy. Do not use height-for-width.

- [ ] **Step 3: Implement deterministic width fitting**

For each fit, use a copied `QFont` and `QFontMetrics.horizontalAdvance()` to test integer pixel sizes from 32 down through 18 against `contentsRect().width()`. Select the largest fitting size. If the complete value still does not fit at 18 px, use `QFontMetrics.elidedText(full_text, Qt.TextElideMode.ElideRight, available_width)` for the visible label only.

Apply the chosen font and style consistently:

```python
label.setStyleSheet(
    f"font-size: {pixel_size}px; font-weight: 800; color: {color};"
)
```

Keep `toolTip()` as `结果摘要：{full_text}` and the accessible description as the complete unelided status. Queue a refit after construction/text changes so the final layout width is used.

- [ ] **Step 4: Simplify the overall display helper**

Make `_set_overall_display` delegate to `_FittedStatusLabel.set_display(...)`. Remove wrapped-minimum-height recalculation, owner traversal, `adjustSize()`, and other vertical resizing logic that is no longer needed. Preserve the existing helper call sites for construction, `_update_summary`, and `preview_manual_judgment`.

- [ ] **Step 5: Keep AI table wrapping unchanged**

Do not modify `_FittedSummaryTable`, `result_history_card_summary_table.wordWrap()`, the widened result column, AI detail preservation, or scrollbar-driven refitting.

- [ ] **Step 6: Run the complete card tests**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -c "import sys; sys.path.append(r'D:\Program Files\Python\Python312\Lib\site-packages'); import pytest, PyQt6.QtCore; raise SystemExit(pytest.main(['tests/test_result_history_card.py', '-v']))"
```

Expected: all card tests PASS.

- [ ] **Step 7: Commit the implementation checkpoint**

```powershell
git add ui/widgets/result_history_card.py tests/test_result_history_card.py
git commit -m "tmp/sdd: fit result history status on one line"
```

### Task 3: Verify Result-History Integrations

**Files:**
- Verify: `ui/widgets/result_history_card.py`
- Verify: `tests/test_result_history_card.py`
- Verify: `tests/test_result_history_module.py`
- Verify: `tests/test_result_dashboard_module.py`
- Verify: `tests/multi_run_integration/test_two_stage_analysis_projection.py`

- [ ] **Step 1: Run directly affected test files**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -c "import sys; sys.path.append(r'D:\Program Files\Python\Python312\Lib\site-packages'); import pytest, PyQt6.QtCore; raise SystemExit(pytest.main(['tests/test_result_history_card.py', 'tests/test_result_history_module.py', 'tests/test_result_dashboard_module.py', 'tests/multi_run_integration/test_two_stage_analysis_projection.py', '-v']))"
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run syntax and diff checks**

```powershell
& '.\.venv\Scripts\python.exe' -m compileall -q ui/widgets/result_history_card.py tests/test_result_history_card.py
git diff --check
```

Expected: both commands exit 0 without errors.

- [ ] **Step 3: Preserve upstream projection contracts**

Confirm that tests asserting upstream parenthesized values such as `summary_payload["overall_label"] == "不通过（漏装）"` remain unchanged. Only widget-level visible text assertions may use `不通过：漏装`.

- [ ] **Step 4: Commit only if verification required scoped corrections**

If a directly affected integration assertion required an approved-format update, commit only that focused correction as a temporary checkpoint.
