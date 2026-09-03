# PLC Workorder Single-Column Layout Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display each PLC as a full-width two-row SN/model group, separated by a subtle horizontal rule, with overflow scrolling only when more than two PLC groups exceed the available viewport.

**Architecture:** `WorkOrderModule` keeps its existing PLC ID/value caches and rebuild trigger, but wraps the PLC content widget in a dedicated scroll area and changes the outer grid from two columns to one full-width column. Separators are presentation-only `QFrame` rows; all PLC data and event behavior remain unchanged.

**Tech Stack:** Python 3, PyQt6 widgets/layouts, pytest.

---

## File Structure

- Modify `ui/modules/workorder.py`: add the PLC-only scroll viewport, build full-width PLC groups and exact separator rows, synchronize scroll policy with PLC count, and bind SN/model edits to the existing compact `PlcField` style.
- Modify `tests/test_workorder_module.py`: replace the old two-column position assertions and add layout, separator, scroll, dynamic-count, and text-clipping regression coverage.

### Task 1: Build and verify the single-column PLC group layout

**Files:**
- Modify: `ui/modules/workorder.py:3-230`
- Modify: `tests/test_workorder_module.py:340-420`

- [ ] **Step 1: Write failing tests for full-width group ordering**

Update imports explicitly:

```python
# ui/modules/workorder.py
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QScrollArea

# tests/test_workorder_module.py
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFrame, QLabel
```

Add a focused test helper rather than relying on implicit database state:

```python
def _module_with_plcs(count: int):
    module, services = _make_module()
    plc_ids = tuple(
        PlcTable.create(plc_name=f"PLC {index + 1}").id
        for index in range(count)
    )
    services.bus.plc_changed.emit()
    return module, services, plc_ids
```

Replace the old `index // 2, index % 2` expectations with explicit single-column positions:

```python
def test_plc_groups_render_full_width_in_one_column(qapp):
    module, services = _make_module()
    first = PlcTable.create(plc_name="PLC A")
    second = PlcTable.create(plc_name="PLC B")
    services.bus.plc_changed.emit()

    first_group = module.plc_sn_layout.itemAtPosition(0, 0).widget()
    separator = module.plc_sn_layout.itemAtPosition(1, 0).widget()
    second_group = module.plc_sn_layout.itemAtPosition(2, 0).widget()

    assert first_group is not None
    assert separator.objectName() == "plc_group_separator"
    assert second_group is not None
    assert module.plc_sn_layout.itemAtPosition(0, 1) is None
    assert module.plc_sn_layout.columnStretch(0) == 1
    assert module.plc_sn_layout.columnStretch(1) == 0
    assert [label.text() for label in first_group.findChildren(QLabel)] == [
        f"PLC{first.id} SN码",
        f"PLC{first.id} 型号",
    ]
```

Also assert each group’s internal grid keeps SN at `(0, 0)/(0, 1)` and model at `(1, 0)/(1, 1)`, and that the input widgets remain the same objects stored in `_plc_sn_edits` / `_plc_model_edits`.

- [ ] **Step 2: Run the position test to verify it fails**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:QT_QPA_PLATFORM='offscreen'
python -m pytest tests/test_workorder_module.py::test_plc_groups_render_full_width_in_one_column -q
```

Expected: FAIL because PLC1 and PLC2 still occupy outer grid positions `(0, 0)` and `(0, 1)`.

- [ ] **Step 3: Write failing tests for exact separators and dynamic counts**

Add parameterized coverage:

```python
@pytest.mark.parametrize(
    ("plc_count", "separator_count"),
    [(0, 0), (1, 0), (2, 1), (3, 2)],
)
def test_plc_group_separator_count_and_style(qapp, plc_count, separator_count):
    module, services = _make_module()
    for index in range(plc_count):
        PlcTable.create(plc_name=f"PLC {index + 1}")
    services.bus.plc_changed.emit()

    separators = module.plc_sn_widget.findChildren(
        QFrame,
        "plc_group_separator",
    )
    assert len(separators) == separator_count
    for separator in separators:
        assert separator.frameShape() == QFrame.Shape.HLine
        assert separator.frameShadow() == QFrame.Shadow.Plain
        assert separator.lineWidth() == 1
        assert separator.height() == 13
        assert "#e2e8f0" in separator.styleSheet()
        assert separator.focusPolicy() == Qt.FocusPolicy.NoFocus
```

- [ ] **Step 4: Run separator tests to verify they fail**

Run: `python -m pytest tests/test_workorder_module.py -q -k "plc_group_separator or plc_groups_render"`

Expected: FAIL because no separator frames or single-column sequence exist.

- [ ] **Step 5: Write failing tests for scroll behavior**

Cover the user-visible viewport rules:

```python
def test_two_plc_groups_never_show_vertical_scrollbar(qapp):
    module, services, _plc_ids = _module_with_plcs(2)
    module.resize(420, 260)
    module.show()
    qapp.processEvents()

    scroll = module.plc_workorder_scroll_area
    assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert scroll.verticalScrollBar().isVisible() is False
    assert scroll.verticalScrollBar().maximum() == 0


def test_three_plc_groups_scroll_only_inside_plc_viewport(qapp):
    module, services, _plc_ids = _module_with_plcs(3)
    scroll = module.plc_workorder_scroll_area
    scroll.setFixedHeight(110)
    module.show()
    qapp.processEvents()

    assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert scroll.verticalScrollBar().maximum() > 0
    assert module.layout().indexOf(module.lbl_phase_summary) > module.layout().indexOf(scroll)
    assert scroll.isAncestorOf(module.plc_sn_widget)
    assert scroll.isAncestorOf(module.lbl_phase_summary) is False
```

Also assert horizontal policy is always `ScrollBarAlwaysOff`, and that changing from three PLCs back to two updates the vertical policy to `ScrollBarAlwaysOff` with no visible bar after event processing.

Add explicit protected-state coverage:

```python
@pytest.mark.parametrize("plc_count", [0, 1, 2])
def test_zero_to_two_plcs_force_vertical_scrollbar_off(qapp, plc_count):
    module, _services, _plc_ids = _module_with_plcs(plc_count)
    assert (
        module.plc_workorder_scroll_area.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    if plc_count == 0:
        empty = module.plc_sn_layout.itemAtPosition(0, 0).widget()
        assert empty.text() == "未配置 PLC"
        assert module.plc_sn_layout.itemAtPosition(0, 1) is None


def test_cached_values_and_post_rebuild_sn_highlight_remain_correct(qapp):
    module, services, (first_id, second_id) = _module_with_plcs(2)
    services.bus.plc_workorder_changed.emit(
        {"plc_id": second_id, "sn": "SN-B", "model": "MODEL-B"}
    )
    PlcTable.create(plc_name="PLC 3")
    services.bus.plc_changed.emit()
    assert module._plc_sn_edits[second_id].text() == "SN-B"
    assert module._plc_model_edits[second_id].text() == "MODEL-B"
    services.bus.plc_workorder_changed.emit(
        {"plc_id": second_id, "sn": "SN-B-2", "model": "MODEL-B"}
    )
    assert "#2563eb" in module._plc_sn_edits[second_id].styleSheet()
```

Retain and update the existing add/delete test so it continues to prove PLC ID ordering, read-only edits, `未读取` placeholders, cached values, and highlight behavior for events received after rebuilds. Do not add a new requirement that transient widget styling itself survive destruction/recreation.

- [ ] **Step 6: Run scroll tests to verify they fail**

Run: `python -m pytest tests/test_workorder_module.py -q -k "plc_groups_never_show_vertical_scrollbar or plc_groups_scroll_only_inside or zero_to_two_plcs_force_vertical_scrollbar_off or cached_values_and_post_rebuild_sn_highlight or scroll_policy"`

Expected: FAIL because `plc_workorder_scroll_area` does not exist.

- [ ] **Step 7: Implement the scroll viewport**

Import `Qt` from `PyQt6.QtCore` and `QFrame`, `QScrollArea` from `PyQt6.QtWidgets`:

```python
self.plc_workorder_scroll_area = QScrollArea()
self.plc_workorder_scroll_area.setObjectName("plc_workorder_scroll_area")
self.plc_workorder_scroll_area.setWidgetResizable(True)
self.plc_workorder_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
self.plc_workorder_scroll_area.setHorizontalScrollBarPolicy(
    Qt.ScrollBarPolicy.ScrollBarAlwaysOff
)
self.plc_workorder_scroll_area.setWidget(self.plc_sn_widget)
layout.addWidget(self.plc_workorder_scroll_area, 1)
layout.addWidget(self.lbl_phase_summary)
```

Set only `plc_sn_layout.setColumnStretch(0, 1)` and remove the second stretched column.

Change the no-PLC empty-state placement from `addWidget(empty_label, 0, 0, 1, 2)` to `addWidget(empty_label, 0, 0, 1, 1)` so it obeys the single-column full-width invariant.

- [ ] **Step 8: Implement full-width groups and exact separators**

Build a monotonically increasing `outer_row`:

```python
outer_row = 0
for index, plc_id in enumerate(plc_ids):
    if index:
        separator = QFrame()
        separator.setObjectName("plc_group_separator")
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setLineWidth(1)
        separator.setFixedHeight(13)
        separator.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        separator.setStyleSheet(
            "QFrame#plc_group_separator { color: #e2e8f0; }"
        )
        self.plc_sn_layout.addWidget(separator, outer_row, 0, 1, 1)
        outer_row += 1

    row_widget = self._build_plc_group(plc_id)
    self.plc_sn_layout.addWidget(row_widget, outer_row, 0, 1, 1)
    outer_row += 1
```

Either extract `_build_plc_group()` or keep the existing local construction, but preserve the internal two-row label/edit layout exactly. Do not change caches or event handling.

- [ ] **Step 9: Implement count-based scroll policy and two-PLC fit**

After rebuilding:

```python
if len(plc_ids) <= 2:
    policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    self.plc_workorder_scroll_area.setMinimumHeight(
        self.plc_sn_widget.sizeHint().height()
    )
else:
    policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded
    self.plc_workorder_scroll_area.setMinimumHeight(0)
self.plc_workorder_scroll_area.setVerticalScrollBarPolicy(policy)
self.plc_sn_widget.adjustSize()
```

If Qt sizing requires ordering adjustments, preserve these invariants: 0–2 PLCs have vertical policy `AlwaysOff` and enough minimum height for their content; 3+ PLCs use `AsNeeded`; horizontal scrolling stays disabled.

- [ ] **Step 10: Run the full WorkOrder tests**

Run: `python -m pytest tests/test_workorder_module.py -q`

Expected: PASS with zero failures.

- [ ] **Step 11: Run affected control and focused feature regressions**

Run:

```powershell
python -m pytest tests/test_workorder_module.py tests/test_control_module.py tests/test_factory_config_selection_dialog.py tests/test_config_module.py -q -k "not fault_types_editor"
```

Expected: PASS with only the four pre-existing fault-type-editor cases deselected by name.

- [ ] **Step 12: Compile and inspect the diff**

Run: `python -m compileall -q ui/modules/workorder.py tests/test_workorder_module.py`

Expected: exit code 0.

Run: `git diff --check HEAD^..HEAD` after the checkpoint commit, or `git diff --check` before it.

Expected: no output and exit code 0.

- [ ] **Step 13: Commit the task checkpoint**

```bash
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "tmp/sdd: stack PLC workorder groups vertically"
```

### Task 2: Bind PLC fields to the compact non-clipping style

**Files:**
- Modify: `ui/modules/workorder.py:230-250`
- Modify: `tests/test_workorder_module.py`

- [ ] **Step 1: Write the failing style-binding regression**

Import the real style geometry types and application stylesheet:

```python
from PyQt6.QtWidgets import QStyle, QStyleOptionFrame
from consts.style import APP_QSS
```

Add a test that applies the same QSS used by `MainWindow` and inspects both kinds of PLC field:

```python
def test_plc_fields_use_compact_style_without_clipping_text(qapp):
    module, _services, plc_ids = _module_with_plcs(2)
    module.setStyleSheet(APP_QSS)
    module.show()
    qapp.processEvents()

    edits = tuple(module._plc_sn_edits.values()) + tuple(
        module._plc_model_edits.values()
    )
    assert len(edits) == 4
    for edit in edits:
        assert edit.objectName() == "PlcField"
        assert 28 <= edit.height() <= 30
        option = QStyleOptionFrame()
        edit.initStyleOption(option)
        content = edit.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents,
            option,
            edit,
        )
        assert content.height() >= edit.fontMetrics().height() + 2
```

This test must use the actual `APP_QSS` rather than duplicating padding values in the test.

- [ ] **Step 2: Run the clipping regression to verify it fails**

Run: `python -m pytest tests/test_workorder_module.py::test_plc_fields_use_compact_style_without_clipping_text -q`

Expected: FAIL because the edits have no `PlcField` object name and use the ordinary `QLineEdit` rule whose natural height is 34 px while the widget is capped at 30 px.

- [ ] **Step 3: Bind both inputs to the existing compact style**

Add the same object name immediately after constructing each input:

```python
sn_edit = QLineEdit()
sn_edit.setObjectName("PlcField")

model_edit = QLineEdit()
model_edit.setObjectName("PlcField")
```

Do not change `APP_QSS`, the 28 px minimum, the 30 px maximum, input widths, row spacing, scroll policy, or any PLC data/event behavior.

- [ ] **Step 4: Run the focused regression to verify it passes**

Run: `python -m pytest tests/test_workorder_module.py::test_plc_fields_use_compact_style_without_clipping_text -q`

Expected: PASS; the `PlcField` content rectangle provides at least 2 px more vertical space than the font height.

- [ ] **Step 5: Run affected regression suites**

Run: `python -m pytest tests/test_workorder_module.py tests/test_control_module.py -q`

Expected: PASS with zero failures.

Run: `python -m pytest tests/test_workorder_module.py tests/test_control_module.py tests/test_factory_config_selection_dialog.py tests/test_config_module.py -q -k "not fault_types_editor"`

Expected: PASS with the four known fault-type-editor tests deselected by name.

- [ ] **Step 6: Compile and inspect the diff**

Run: `python -m compileall -q ui/modules/workorder.py tests/test_workorder_module.py`

Expected: exit code 0.

Run: `git diff --check HEAD^..HEAD` after the checkpoint commit, or `git diff --check` before it.

Expected: no output and exit code 0.

- [ ] **Step 7: Commit the task checkpoint**

```bash
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "tmp/sdd: prevent PLC field text clipping"
```
