# AI Tool Management Layout Alignment Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the AI tool management detail pane use the same scroll-content layout pattern as the config page so tall windows do not stretch form rows vertically.

**Architecture:** Update the right-side detail pane in `ui/modules/ai_tool_management.py` to wrap the editable form in a `QScrollArea` with a compact scroll-content widget and bottom stretch, while keeping the save row outside the scroll area. Extend the existing UI tests to verify the scroll wrapper structure, save-row placement, tall-window geometry behavior, and preservation of the fixed single-line field rows.

**Tech Stack:** Python, PyQt6, pytest

---

### Task 1: Align AI Tool Management Detail Layout With Config Page Pattern

**Files:**
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Write the failing regression tests for the scroll layout structure**

Add tests in `tests/test_ai_tool_management_module.py` that assert:

```python
assert hasattr(module, "detail_scroll")
assert module.detail_scroll.widgetResizable() is True
assert module.detail_scroll.widget() is not None
assert module.detail_scroll.frameShape().name == "NoFrame"
assert module.detail_scroll.parentWidget() == module.detail
save_row = module.btn_save.parentWidget()
detail_layout = module.detail.layout()
assert save_row.parentWidget() == module.detail
assert detail_layout.indexOf(module.detail_scroll) >= 0
assert detail_layout.indexOf(save_row) > detail_layout.indexOf(module.detail_scroll)
assert save_row.parentWidget() != module.detail_scroll.widget()
```

- [ ] **Step 2: Run the targeted layout tests to verify the new assertions fail first**

Run: `pytest tests/test_ai_tool_management_module.py -q`

Expected: FAIL because `AiToolManagementModule` does not yet expose `detail_scroll` and still lays out the form directly in the detail container.

- [ ] **Step 3: Implement the minimal right-pane layout change**

Update `ui/modules/ai_tool_management.py` so `_init_ui()`:

```python
scroll_content = QWidget()
scroll_lay = QVBoxLayout(scroll_content)
scroll_lay.setContentsMargins(0, 0, 0, 0)
scroll_lay.setSpacing(10)
scroll_lay.addWidget(form_host)
scroll_lay.addStretch(1)

self.detail_scroll = QScrollArea()
self.detail_scroll.setWidgetResizable(True)
self.detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
self.detail_scroll.setWidget(scroll_content)
detail_lay.addWidget(self.detail_scroll, 1)
detail_lay.addWidget(save_row)
```

Keep:

```python
form_lay.setVerticalSpacing(8)
self._apply_fixed_row_heights()
```

Do not change validation, save logic, or unrelated UI behavior.

- [ ] **Step 4: Add the tall-window geometry regression assertion**

Extend `tests/test_ai_tool_management_module.py` with a test that:

```python
module.btn_new.click()
module.resize(1280, 1400)
module.show()
qapp.processEvents()

form_host = module.ed_toolchain_id.parentWidget()
assert form_host.height() < module.detail.height()
assert form_host.height() <= form_host.sizeHint().height() + 40
```

Also keep the existing checks that `AI 包目录` and `默认超时` remain fixed-height single-line rows with visible controls.

- [ ] **Step 5: Run the focused tests to verify the layout fix passes**

Run: `pytest tests/test_ai_tool_management_module.py -q`

Expected: PASS

- [ ] **Step 6: Commit the task**

```bash
git add ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git commit -m "fix: align AI tool management detail layout"
```
