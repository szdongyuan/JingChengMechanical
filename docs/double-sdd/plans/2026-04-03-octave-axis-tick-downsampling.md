# Octave Axis Tick Downsampling Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the visible octave x-axis tick labels to the approved subset without changing octave bar rendering or any other chart behavior.

**Architecture:** Keep the octave chart on its current zero-based categorical axis and change only the tick-label generation inside `OctaveBarChartWidget`. Add a focused widget regression that verifies the visible label subset while leaving payloads, bar positions, overlays, and markers unchanged.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, pytest

---

## File Structure

- Modify: `ui/widgets/analysis_charts.py`
  - Keep `OctaveBarChartWidget` bar/index rendering intact.
  - Replace the current "label every center frequency" bottom-axis behavior with approved sparse labels only.
- Modify: `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
  - Add focused regression coverage for the octave axis tick labels on a dense center-frequency payload.

### Task 1: Downsample Octave X-Axis Tick Labels

**Files:**
- Modify: `ui/widgets/analysis_charts.py`
- Test: `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`

- [ ] **Step 1: Write the failing octave tick-label regression**

Add a focused widget test that feeds a dense octave frequency list and asserts only the approved labels appear:

```python
def test_octave_bar_chart_widget_downsamples_visible_axis_labels_to_approved_subset(qapp):
    widget = OctaveBarChartWidget()

    widget.set_bands(
        center_freqs_hz=[
            31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0,
            200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0,
            1250.0, 1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0,
            6300.0, 8000.0, 10000.0, 12500.0, 16000.0, 20000.0,
        ],
        levels_db=[0.0] * 29,
        limit_bands=[],
    )

    ticks = widget.plot_item.getAxis("bottom")._tickLevels
    labels = [text for _value, text in ticks[0]]

    assert labels == [
        "", "40", "", "", "80", "", "", "160", "", "", "315", "", "", "630",
        "", "", "1250", "", "", "2500", "", "", "5000", "", "", "10000", "", "16000", "",
    ]
    assert widget.band_payload()["center_freqs_hz"] == [
        31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0,
        200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0,
        1250.0, 1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0,
        6300.0, 8000.0, 10000.0, 12500.0, 16000.0, 20000.0,
    ]
    assert widget._bar_item is not None
    assert list(np.asarray(widget._bar_item.opts["x"], dtype=float)) == list(np.arange(29, dtype=float))
    assert len(np.asarray(widget._bar_item.opts["height"], dtype=float)) == 29
```

- [ ] **Step 2: Run the focused widget test to verify it fails**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py -q -k "octave_bar_chart_widget_downsamples_visible_axis_labels"
```

Expected: FAIL because the octave widget currently labels every center frequency.

- [ ] **Step 3: Implement the axis-only tick-label filtering**

Update `OctaveBarChartWidget.set_bands(...)` so the bottom-axis tick list uses the approved label subset only:

```python
visible_labels_hz = {40.0, 80.0, 160.0, 315.0, 630.0, 1250.0, 2500.0, 5000.0, 10000.0, 16000.0}
ticks = [
    (
        float(index),
        self._format_frequency_label(freq_hz) if freq_hz in visible_labels_hz else "",
    )
    for index, freq_hz in enumerate(normalized_frequencies)
]
self.plot_item.getAxis("bottom").setTicks([ticks])
```

Implementation notes:

- keep the existing x positions `0..N-1`
- do not filter out any bars or payload values
- do not change overlays, markers, widths, colors, or axis range policy
- keep the change localized to the octave widget

- [ ] **Step 4: Run the focused chart tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py -q -k "octave_bar_chart_widget_downsamples_visible_axis_labels or test_octave_bar_chart_widget_accepts_limit_band_markers or test_octave_bar_chart_widget_accepts_golden_overlay_payload or test_octave_bar_chart_widget_keeps_negative_delta_bars_anchored_to_zero"
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py -q -k "test_octave_bar_chart_widget_uses_zero_based_bar_center_range or test_octave_bar_chart_widget_limit_markers_do_not_expand_zero_based_x_range"
```

Expected: PASS

- [ ] **Step 5: Run the octave committed-module smoke tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "octave_module"
```

Expected: PASS

- [ ] **Step 6: Commit the axis-only UI change**

```bash
git add ui/widgets/analysis_charts.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py
git commit -m "fix(ui): downsample octave axis tick labels"
```
