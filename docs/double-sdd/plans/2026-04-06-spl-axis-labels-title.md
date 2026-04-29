# SPL Axis Labels And Title Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit axis labels to the SPL chart and rename the visible SPL card title to `声压级 (SPL)` without changing any other chart behavior.

**Architecture:** Keep the existing `SplModule` on the shared `LineChartWidget` and localize the behavior change to module-level axis-label setup plus the card-title string in `app/main_window.py`. Add narrow regression tests that assert the labels on the SPL module and the new title on the main analysis page.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, pytest

---

## File Structure

- Modify: `ui/modules/spl.py`
  - assign stable axis labels for the SPL chart during initialization and after clearing/rendering
- Modify: `app/main_window.py`
  - replace the SPL card title string with `声压级 (SPL)`
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - extend SPL module coverage to assert the new axis labels
- Modify: `tests/test_main_window.py`
  - add or update focused coverage for the renamed SPL card title

### Task 1: Add SPL Axis Labels And Rename The Card Title

**Files:**
- Modify: `ui/modules/spl.py`
- Modify: `app/main_window.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing UI regressions**

Add focused assertions for the new user-visible text:

```python
def test_spl_module_renders_committed_chart_for_selected_channel(qapp):
    ...
    assert module.plot.plot_item.getAxis("bottom").labelText == "Time (s)"
    assert module.plot.plot_item.getAxis("left").labelText == "SPL (dB)"


def test_spl_module_keeps_axis_labels_when_committed_payload_has_no_series_points(qapp):
    ...
    _assert_empty_chart_visible(module, qapp)
    assert module.plot.plot_item.getAxis("bottom").labelText == "Time (s)"
    assert module.plot.plot_item.getAxis("left").labelText == "SPL (dB)"


def test_test_page_uses_renamed_spl_card_title(...):
    ...
    assert _card_title(_test_page_grid_card(window, 2, 1)) == "声压级 (SPL)"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "test_spl_module_renders_committed_chart_for_selected_channel"
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "test_spl_module_keeps_axis_labels_when_committed_payload_has_no_series_points"
pytest tests/test_main_window.py -q -k "test_test_page_uses_renamed_spl_card_title"
```

Expected: FAIL because the SPL module currently clears back to a blank unlabeled chart and the main-window card title is still `SPL 时间曲线 (A计权)`.

- [ ] **Step 3: Implement the localized UI text change**

Update the SPL module to set labels on init and after clearing/rendering, following the same pattern used by `LoudnessModule`:

```python
self.plot = LineChartWidget()
self.plot.plot_item.setLabel("bottom", "Time (s)")
self.plot.plot_item.setLabel("left", "SPL (dB)")
```

Also change the main-window card title string:

```python
grid.addWidget(Card("声压级 (SPL)", self.spl), 2, 1)
```

Implementation notes:

- keep the existing SPL payload contract unchanged
- keep the existing render-phase and selected-channel behavior unchanged
- restore the labels after `_clear_plot()` so the empty chart still shows them
- do not modify shared `LineChartWidget` defaults because the requirement is SPL-specific

- [ ] **Step 4: Run the focused tests to verify they pass**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "test_spl_module_renders_committed_chart_for_selected_channel or test_spl_module_keeps_axis_labels_when_committed_payload_has_no_series_points"
pytest tests/test_main_window.py -q -k "test_test_page_uses_renamed_spl_card_title"
```

Expected: PASS

- [ ] **Step 5: Run a small related smoke slice**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "spl_module or loudness_module"
```

Expected: PASS
