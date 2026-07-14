# Primary Config Badge Vertical Alignment Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vertically center the “首选” badge in the first factory configuration row without changing the existing row height or configuration-list behavior.

**Architecture:** Keep the existing Qt layout and `AlignVCenter` behavior, but give the row a feasible 20 px badge area with symmetric 3 px internal padding. Represent the `QListWidget::item` margin and border as shared module constants, and add their derived 6 px vertical decoration allowance when assigning each item's size hint so the overall 32 px row density remains unchanged.

**Tech Stack:** Python 3, PyQt6 widgets/layouts, pytest

---

## File Structure

- Modify `ui/widgets/factory_config_list.py`: define item-decoration constants, use them in the item stylesheet, reduce row-widget vertical padding, and include the decoration allowance in item size hints.
- Modify `tests/test_factory_config_list.py`: add actual-geometry regression assertions to the existing primary-badge height test.

### Task 1: Center the primary badge while preserving row density

**Files:**
- Modify: `ui/widgets/factory_config_list.py:3,32-33,116-121,304-320`
- Test: `tests/test_factory_config_list.py:153-165`

- [ ] **Step 1: Add failing geometry and density assertions**

Extend `test_factory_config_list_primary_badge_has_non_clipping_height` after obtaining `row_widget`:

```python
    item = widget.item(0)
    row_widget = widget.itemWidget(item)

    row_center_y = row_widget.rect().center().y()
    badge_center_y = row_widget.lbl_primary.geometry().center().y()

    assert abs(badge_center_y - row_center_y) <= 1
    assert item.sizeHint().height() == 32
    assert row_widget.lbl_primary.minimumHeight() >= 20
    assert row_widget.lbl_primary.height() >= row_widget.lbl_primary.minimumHeight()
    assert item.sizeHint().height() >= row_widget.sizeHint().height()
```

- [ ] **Step 2: Run the regression test and verify that it fails for the reported offset**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; $env:PYTEST_QT_API='pyqt6'; python -m pytest tests/test_factory_config_list.py::test_factory_config_list_primary_badge_has_non_clipping_height -v
```

Expected: FAIL on `abs(badge_center_y - row_center_y) <= 1`, with the current center delta measuring 3 px. The existing 32 px item-height assertion should pass.

- [ ] **Step 3: Introduce shared item-decoration constants**

Near the imports in `ui/widgets/factory_config_list.py`, add:

```python
_ITEM_VERTICAL_MARGIN = 2
_ITEM_BORDER_WIDTH = 1
_ITEM_VERTICAL_DECORATION = 2 * (
    _ITEM_VERTICAL_MARGIN + _ITEM_BORDER_WIDTH
)
```

Import `QSize` from `PyQt6.QtCore`:

```python
from PyQt6.QtCore import QSize, Qt, pyqtSignal
```

- [ ] **Step 4: Make the row content area geometrically feasible**

Change the row layout margins so the 20 px badge has symmetric padding inside the existing 26 px row widget:

```python
        layout.setContentsMargins(8, 3, 8, 3)
```

Keep `self.lbl_primary.setMinimumHeight(20)` and `Qt.AlignmentFlag.AlignVCenter` unchanged.

- [ ] **Step 5: Tie item styling and size allocation to the same constants**

Build the item stylesheet with the constants while leaving all other style values unchanged:

```python
            f"QListWidget::item {{ border: {_ITEM_BORDER_WIDTH}px solid transparent; "
            f"margin: {_ITEM_VERTICAL_MARGIN}px 0; padding: 0; }}"
```

When rebuilding each list item, preserve room for the outer decoration:

```python
            item.setSizeHint(
                widget.sizeHint() + QSize(0, _ITEM_VERTICAL_DECORATION)
            )
```

- [ ] **Step 6: Run the focused regression test and verify that it passes**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; $env:PYTEST_QT_API='pyqt6'; python -m pytest tests/test_factory_config_list.py::test_factory_config_list_primary_badge_has_non_clipping_height -v
```

Expected: PASS; the badge-to-row center delta is no more than 1 px and the item height remains 32 px.

- [ ] **Step 7: Prove the regression test detects the original implementation**

Temporarily restore the original vertical margins and original `item.setSizeHint(widget.sizeHint())`, then rerun the focused test.

Expected: FAIL on the center-delta assertion with a 3 px offset.

Restore the fix and rerun the focused test.

Expected: PASS.

- [ ] **Step 8: Run the complete factory configuration list test module**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'; $env:PYTEST_QT_API='pyqt6'; python -m pytest tests/test_factory_config_list.py -v
```

Expected: all tests pass, covering primary selection, checkbox behavior, drag previews, and drag/drop ordering.

- [ ] **Step 9: Inspect the scoped diff and commit**

Run:

```powershell
git diff --check
git diff -- ui/widgets/factory_config_list.py tests/test_factory_config_list.py
git status --short
git add ui/widgets/factory_config_list.py tests/test_factory_config_list.py
git commit -m "fix: center primary configuration badge"
```

Expected: only the two scoped implementation files are included in the task commit; unrelated workspace files remain untouched.
