# Octave Bar Edge Padding Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure the octave frequency bar chart fully displays the first and last bars with small horizontal blank space.

**Architecture:** Add a dedicated x-axis policy for `OctaveBarChartWidget` instead of changing the shared line/STFT x-axis helper. Keep bar width, tick labels, marker placement, payloads, and y-axis auto-range unchanged.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, pytest

---

## File Structure

- Modify: `ui/widgets/analysis_charts.py`
- Modify: `tests/test_analysis_chart_modules/test_zero_based_axes.py`

## Task 1: Add Octave Bar Edge Padding

**Files:**
- Modify: `ui/widgets/analysis_charts.py`
- Test: `tests/test_analysis_chart_modules/test_zero_based_axes.py`

- [ ] **Step 1: Write the failing chart range tests**

Update the octave-specific expectations in `tests/test_analysis_chart_modules/test_zero_based_axes.py` so they assert:

```python
_assert_x_range(widget, (-0.5, 2.5))
```

for the existing three-bar octave chart cases, including the limit-marker case.

Update the single-bar octave chart case so it asserts:

```python
_assert_x_range(widget, (-0.5, 0.5))
```

Update the octave assertion in `test_chart_widgets_programmatic_refresh_reset_preserve_zero_based_axes` so the refreshed three-bar octave chart also asserts:

```python
_assert_x_range(octave, (-0.5, 2.5))
```

Keep the empty/clear-data fallback assertion at:

```python
_assert_x_range(octave, (0.0, 1.0))
```

- [ ] **Step 2: Run the focused tests and confirm the current failure**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py -q
```

Expected: FAIL because the current implementation uses bar centers as the x-axis edges.

- [ ] **Step 3: Implement a dedicated octave bar x-axis policy**

In `ui/widgets/analysis_charts.py`, keep `_apply_x_axis_policy()` unchanged for non-bar charts.

Add constants near `OctaveBarChartWidget`:

```python
_OCTAVE_BAR_WIDTH = 0.6
_OCTAVE_BAR_EDGE_MARGIN = 0.2
```

Use `_OCTAVE_BAR_WIDTH` when creating `pg.BarGraphItem`.

Add a helper on `OctaveBarChartWidget`:

```python
def _apply_bar_x_axis_policy(self, *, x_values: np.ndarray) -> None:
    if x_values.size == 0:
        self._apply_x_axis_policy(payload_values=None)
        return
    edge_padding = (_OCTAVE_BAR_WIDTH / 2.0) + _OCTAVE_BAR_EDGE_MARGIN
    view_box = self.plot_item.vb
    view_box.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
    view_box.setXRange(
        float(x_values[0]) - edge_padding,
        float(x_values[-1]) + edge_padding,
        padding=0.0,
    )
```

Call this helper from `set_bands()` after rendering bars and markers, replacing the current `_apply_x_axis_policy(payload_values=x_values.tolist())` call.

Keep `clear_data()` using `_apply_x_axis_policy(payload_values=None)` so empty charts retain the existing fallback range.

- [ ] **Step 4: Re-run focused chart tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the related chart widget regression tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add ui/widgets/analysis_charts.py tests/test_analysis_chart_modules/test_zero_based_axes.py docs/double-sdd/specs/2026-04-20-octave-bar-edge-padding-design.md docs/double-sdd/plans/2026-04-20-octave-bar-edge-padding.md
git commit -m "fix(charts): add octave bar edge padding"
```
