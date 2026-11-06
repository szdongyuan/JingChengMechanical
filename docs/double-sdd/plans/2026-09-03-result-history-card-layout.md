# Result History Card Layout Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the result-history card's overall conclusion visually dominant and show complete wrapped AI analysis results at the production 360 px panel width.

**Architecture:** Keep the existing `ResultHistoryCard` data flow and object identities, but split the summary caption from the large plain-text conclusion label and centralize its styling. Replace equal-width table columns with compact fixed metadata columns plus a stretched result column, and make `_FittedSummaryTable` calculate row and table heights from wrapped result text after layout.

**Tech Stack:** Python 3, PyQt6 widgets, pytest, offscreen Qt tests

---

## File Structure

- Modify `ui/widgets/result_history_card.py`: status-area construction and updates, result-column sizing, wrapped-row fitting, and preservation of detailed AI result text.
- Modify `tests/test_result_history_card.py`: focused regression coverage for the large status treatment, long AI result wrapping, and in-place refitting.

### Task 1: Add Result Card Layout Regression Coverage

**Files:**
- Modify: `tests/test_result_history_card.py`
- Test: `tests/test_result_history_card.py`

- [ ] **Step 1: Replace conflicting combined-summary assertions and write a failing large-status test**

Update the existing tests that search for a single combined `结果摘要：...` label so they instead assert that the visible caption is `结果摘要` and the existing `result_history_card_summary_overall` object contains the expected conclusion. Preserve the same phase/conclusion/fault-type coverage.

Add a focused parameterized test for terminal NG and no-result cards. Locate `result_history_card_summary_overall` and verify the label contains only the conclusion text, remains plain text, is horizontally centered, permits wrapping, expands horizontally, has a minimum height suitable for a 32 px font, and has a style containing `font-size: 32px` and the red fail color. Also verify a separate visible caption still says `结果摘要`.

```python
@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        (ResultConclusion.NG, "不通过（风扇异常）"),
        (ResultConclusion.NO_RESULT, "无结果"),
    ],
)
def test_failure_card_renders_large_centered_wrapping_overall_status(
    qapp, conclusion, expected
):
    card = ResultHistoryCard(
        _snapshot(
            conclusion=conclusion,
            fault_type="风扇异常" if conclusion is ResultConclusion.NG else None,
        )
    )
    overall = card.findChild(QLabel, "result_history_card_summary_overall")
    caption = card.findChild(QLabel, "result_history_card_summary_caption")

    assert caption.text() == "结果摘要"
    assert overall.text() == expected
    assert overall.textFormat() is Qt.TextFormat.PlainText
    assert overall.accessibleName() == "结果摘要总结果"
    assert overall.wordWrap() is True
    assert overall.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert overall.minimumHeight() >= 48
    assert "font-size: 32px" in overall.styleSheet()
    assert "#d93025" in overall.styleSheet().lower()
```

- [ ] **Step 2: Write a failing long-AI-result fit test**

Construct a 360 px-wide terminal card whose AI row has a detailed result such as `NG (FAN BLADE INSTALLATION ABNORMAL)` and verify the exact text is retained, the result column is wider than the current column, word wrapping is enabled, the AI row grows beyond the compact base height, the table height contains the header plus every row, and neither scrollbar is visible after processing layout events.

```python
assert table.item(5, 3).text() == long_ai_result
assert table.item(5, 3).toolTip() == long_ai_result
assert table.wordWrap() is True
assert table.columnWidth(3) > table.columnWidth(2)
assert table.rowHeight(5) > 23
assert sum(table.rowHeight(row) for row in range(6)) <= table.viewport().height()
assert not table.horizontalScrollBar().isVisible()
assert not table.verticalScrollBar().isVisible()
```

- [ ] **Step 3: Write a failing in-place update refit test**

Create the card with a short AI result, record the initial AI-row and table heights, update the same card through `update_snapshot(...)` with a payload containing a long AI result, process events, and verify the same table instance now contains the complete string in both `text()` and `toolTip()` and has increased row/table heights.

- [ ] **Step 4: Replace the obsolete fixed-row regression expectations**

Update `test_summary_table_keeps_compact_fixed_rows_and_two_decimal_values` so it retains the two-decimal formatting, no-scrollbar, and viewport-fit assertions but no longer requires every row to equal 23 px. Assert non-wrapped rows remain at least the 23 px baseline and the long detailed result row grows. Update any test name that still claims all rows are fixed.

- [ ] **Step 5: Run the new tests and verify they fail**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_result_history_card.py -k "large_centered_wrapping_overall_status or long_ai_result or in_place_update_refits or wrapped_rows" -v
```

Expected: FAIL because the status is still a fixed one-line label, detailed failure text is collapsed, table word wrapping is disabled, and all rows are fixed at 23 px.

### Task 2: Implement the Large Status Area and Wrapped AI Result

**Files:**
- Modify: `ui/widgets/result_history_card.py`
- Test: `tests/test_result_history_card.py`

- [ ] **Step 1: Build a stable large status area**

In `_add_summary`, add a full-width status container with a centered plain-text caption label named `result_history_card_summary_caption` and keep `result_history_card_summary_overall` on the conclusion value label. Configure the conclusion label with centered alignment, wrapping, an expanding horizontal size policy, and a minimum height of at least 48 px.

Add one helper that updates text, tooltip, and semantic color without duplicating logic between construction, summary updates, and manual judgment updates. The helper must apply the approved 32 px bold style:

```python
@staticmethod
def _set_overall_display(label: QLabel, text: str, color: str) -> None:
    label.setText(text)
    label.setToolTip(f"结果摘要：{text}")
    label.setStyleSheet(
        f"font-size: 32px; font-weight: 800; color: {color};"
    )
```

Use `#188038` for pass, `#d93025` for fail/no-result, and `#424242` for neutral/in-progress states. Preserve the existing `结果摘要总结果` accessible name on `result_history_card_summary_overall`. Update `preview_manual_judgment` and `_update_summary` to set the conclusion text directly instead of partitioning the old combined `结果摘要：...` string.

- [ ] **Step 2: Give the result column usable width**

Keep the production module width unchanged. In `_add_summary`, replace equal stretching of all four columns with fixed compact widths for columns 0-2 and stretch mode for column 3. Use widths that preserve the existing Chinese labels and leave the largest share for results at a 360 px card width, for example 76 px, 70 px, and 54 px for the first three columns.

```python
header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
table.setColumnWidth(0, 76)
table.setColumnWidth(1, 70)
table.setColumnWidth(2, 54)
table.setWordWrap(True)
```

- [ ] **Step 3: Fit wrapped result rows and the full table**

Extend `_FittedSummaryTable` so its public fit method first calculates the required height of each result-cell string using the actual result-column width and the table font metrics, clamps every row to at least 23 px, then sets the fixed table height from the header, all actual row heights, frame, and existing fit allowance. Run the fit immediately and queue one zero-delay refit so the final card width is available after layout.

```python
def schedule_initial_fit(self) -> None:
    self._fit_rows()
    QTimer.singleShot(0, self._fit_rows)

def _fit_rows(self) -> None:
    available_width = max(1, self.columnWidth(3) - 12)
    for row in range(self.rowCount()):
        item = self.item(row, 3)
        text = item.text() if item is not None else ""
        bounds = self.fontMetrics().boundingRect(
            QRect(0, 0, available_width, 10_000),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            text,
        )
        self.setRowHeight(row, max(self._ROW_HEIGHT, bounds.height() + 8))
    self.setFixedHeight(
        self.horizontalHeader().height()
        + sum(self.rowHeight(row) for row in range(self.rowCount()))
        + self.frameWidth() * 2
        + 2
    )
```

Use the existing call after `_update_summary` so in-place payload changes refit as well as initial construction.

- [ ] **Step 4: Preserve detailed AI result text**

Change `_summary_result_text` to accept a `preserve_details` flag. Continue translating exact `PASS`/`FAIL` values and keeping the current compact normalization for non-AI rows, but return non-empty detailed AI text unchanged. Pass `preserve_details=True` only when `item_key == "ai"` in both initial and update rendering loops.

```python
if preserve_details:
    return text
if "不通过" in text:
    return "不通过"
```

- [ ] **Step 5: Run the focused tests and verify they pass**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_result_history_card.py -v
```

Expected: all result-history-card tests PASS.

- [ ] **Step 6: Commit the implementation checkpoint**

```powershell
git add ui/widgets/result_history_card.py tests/test_result_history_card.py
git commit -m "tmp/sdd: improve result history card layout"
```

### Task 3: Verify Result History Integration

**Files:**
- Verify: `ui/widgets/result_history_card.py`
- Verify: `ui/modules/result_history.py`
- Test: `tests/test_result_history_card.py`
- Test: `tests/test_result_history_module.py`

- [ ] **Step 1: Run the card and module integration tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_result_history_card.py tests/test_result_history_module.py -v
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 2: Run source hygiene checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q ui/widgets/result_history_card.py tests/test_result_history_card.py
git diff --check
```

Expected: both commands exit with code 0 and produce no errors.

- [ ] **Step 3: Commit any verification-only adjustments**

If integration verification required code or test changes, commit only those scoped files:

```powershell
git add ui/widgets/result_history_card.py tests/test_result_history_card.py
git commit -m "tmp/sdd: finalize result card layout verification"
```
