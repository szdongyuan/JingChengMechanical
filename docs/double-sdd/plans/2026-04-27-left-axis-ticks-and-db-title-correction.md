# Homepage Left Axis Ticks And dB Title Correction Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore homepage chart y-axis ticks while hiding only y-axis title text, and correct FFT/octave homepage title units to `dB`.

**Architecture:** Change the chart-widget opt-in from hiding the whole left axis to hiding only the left-axis label/title text. Keep default widget behavior unchanged, update homepage modules to use the corrected opt-in name/semantics, and update UI tests to assert visible ticks plus hidden axis title.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, pytest

---

## Source Spec

- `docs/double-sdd/specs/2026-04-27-left-axis-ticks-and-db-title-correction-design.md`

## Execution Root

Run commands from:

```text
D:\Work\jingchengpython\JingChengMechanical\.worktrees\homepage-left-axis-chinese-ui
```

## File Map

- Modify: `ui/widgets/analysis_charts.py`
  - replace full left-axis hiding with left-axis-title hiding while keeping ticks visible
- Modify: `ui/modules/stft.py`
- Modify: `ui/modules/spectrum.py`
- Modify: `ui/modules/spl.py`
- Modify: `ui/modules/loudness.py`
- Modify: `ui/modules/octave.py`
  - update homepage chart constructor opt-in if the option is renamed
- Modify: `app/main_window.py`
  - update FFT and octave card titles
- Modify: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
  - assert default compatibility and opted-in axis-title hiding
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - assert homepage modules keep left axis visible while hiding title text
- Modify: `tests/test_main_window.py`
  - update title expectations to `FFT频谱（dB）` and `倍频程（dB）`

## Task 1: Correct Homepage Axis Opt-In And Titles

**Files:**
- Modify: `ui/widgets/analysis_charts.py`
- Modify: `ui/modules/stft.py`
- Modify: `ui/modules/spectrum.py`
- Modify: `ui/modules/spl.py`
- Modify: `ui/modules/loudness.py`
- Modify: `ui/modules/octave.py`
- Modify: `app/main_window.py`
- Test: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Update tests first**

In `tests/test_analysis_chart_modules/test_zero_based_axes.py`, replace the
old hidden-axis assertion helper with a helper that proves ticks and tick values
are still enabled:

```python
def _assert_left_axis_title_hidden_ticks_visible(widget) -> None:
    left_axis = widget.plot_item.getAxis("left")
    assert left_axis.isVisible() is True
    assert left_axis.labelText == ""
    assert left_axis.style.get("showValues") is True
    assert left_axis.style.get("tickLength") not in (None, 0)
    assert left_axis.tickStrings([0.0, 1.0], 1.0, 1.0) == ["0", "1"]
    assert widget.plot_item.getAxis("bottom").isVisible() is True
```

Keep the default-visible helper, and add an explicit default label assertion:

```python
def _assert_left_axis_visible(widget) -> None:
    assert widget.plot_item.getAxis("left").isVisible() is True


def _assert_default_left_axis_label_refresh_visible(widget) -> None:
    widget.plot_item.setLabel("left", "Refreshed", units="dB")
    left_axis = widget.plot_item.getAxis("left")
    assert left_axis.isVisible() is True
    assert left_axis.labelText == "Refreshed"
    assert left_axis.style.get("showValues") is True
```

Update opt-in tests so they construct widgets with the corrected option name,
preferably `hide_left_axis_label=True`, and assert:

- left axis visible;
- `left_axis.labelText == ""`;
- `left_axis.style.get("showValues") is True`;
- `left_axis.style.get("tickLength") not in (None, 0)`;
- bottom axis visible.

Update the label-refresh test so `setLabel("left", "Refreshed", units="dB")`
does not make the title visible for opted-in widgets, while ticks/tick values
remain enabled.

Update `tests/test_analysis_chart_modules/test_committed_chart_modules.py` so
homepage modules assert the same title-hidden/ticks-visible behavior.

Update `tests/test_main_window.py` title expectations:

```python
assert _card_title(_test_page_grid_card(window, 1, 2)) == "FFT频谱（dB）"
assert _card_title(_test_page_grid_card(window, 2, 2)) == "倍频程（dB）"
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py -q -k "left_axis or chinese_chart_titles"
```

Expected: FAIL before implementation because the current helper hides the whole
left axis and titles still use `声压级dB`.

- [ ] **Step 3: Implement chart-widget opt-in semantics**

In `ui/widgets/analysis_charts.py`, replace the previous `hide_left_axis`
behavior with a title-specific option. Preferred implementation:

```python
class _BasePlotChart(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis_label: bool = False,
    ):
        super().__init__(parent)
        self._hide_left_axis_label = bool(hide_left_axis_label)
        ...
        if self._hide_left_axis_label:
            self._install_left_axis_hidden_label_guard()
            self.apply_left_axis_label_hidden_style()

    def apply_left_axis_label_hidden_style(self) -> None:
        left_axis = self.plot_item.getAxis("left")
        left_axis.show()
        left_axis.setStyle(showValues=True)
        self._original_set_label("left", "")
```

Guard against recursion when wrapping `plot_item.setLabel`. The wrapper should
call the original `setLabel` first, then clear the label using the original
method if the axis is `"left"` and `_hide_left_axis_label` is true.

Thread the `hide_left_axis_label` keyword through:

- `StftImageChartWidget`
- `LineChartWidget`
- `FftLineChartWidget`
- `OctaveBarChartWidget`

After each subclass sets a left label (`StftImageChartWidget`,
`FftLineChartWidget`, `OctaveBarChartWidget`), call
`apply_left_axis_label_hidden_style()` when opted in so the title is blank while
ticks remain visible.

Do not change:

- bottom-axis visibility;
- x/y viewbox range policy;
- payload structures;
- chart data rendering.

- [ ] **Step 4: Update homepage module constructors**

Update modules to pass the corrected option name:

```python
FftLineChartWidget(hide_left_axis_label=True)
LineChartWidget(hide_left_axis_label=True)
OctaveBarChartWidget(hide_left_axis_label=True)
StftImageChartWidget(self, hide_left_axis_label=True)
```

Affected files:

- `ui/modules/spectrum.py`
- `ui/modules/spl.py`
- `ui/modules/loudness.py`
- `ui/modules/octave.py`
- `ui/modules/stft.py`

- [ ] **Step 5: Update homepage card titles**

In `app/main_window.py`, update only these titles:

```python
Card("FFT频谱（dB）", self.spectrum)
Card("倍频程（dB）", self.octave)
```

Leave `声压级（dB）`, `STFT时频图（频率Hz，幅值dB）`, and dynamic loudness titles
unchanged.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py -q -k "left_axis or chinese_chart_titles or chart_titles"
```

Expected: PASS

- [ ] **Step 7: Run required regression command**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py tests/test_result_module.py -q
```

Expected: PASS

- [ ] **Step 8: Commit**

Run:

```powershell
git add -f docs/double-sdd/specs/2026-04-27-left-axis-ticks-and-db-title-correction-design.md docs/double-sdd/plans/2026-04-27-left-axis-ticks-and-db-title-correction.md
git add ui/widgets/analysis_charts.py ui/modules/stft.py ui/modules/spectrum.py ui/modules/spl.py ui/modules/loudness.py ui/modules/octave.py app/main_window.py tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py
git commit -m "fix(ui): keep homepage y-axis ticks visible"
```

## Review Gate

- [ ] **Step 1: Dispatch paired code reviews**

After implementation, dispatch:

```text
spec-code-reviewer
Description: Restore homepage y-axis ticks while hiding y-axis titles, and correct FFT/octave card title units to dB.
Spec: docs/double-sdd/specs/2026-04-27-left-axis-ticks-and-db-title-correction-design.md
Plan: docs/double-sdd/plans/2026-04-27-left-axis-ticks-and-db-title-correction.md
Base: <pre-task HEAD>
Head: <post-task HEAD>
```

```text
quality-code-reviewer
Description: Restore homepage y-axis ticks while hiding y-axis titles, and correct FFT/octave card title units to dB.
Spec: docs/double-sdd/specs/2026-04-27-left-axis-ticks-and-db-title-correction-design.md
Plan: docs/double-sdd/plans/2026-04-27-left-axis-ticks-and-db-title-correction.md
Base: <pre-task HEAD>
Head: <post-task HEAD>
```

- [ ] **Step 2: Evaluate review results**

Proceed only after spec compliance passes and any confirmed current-pass quality
defects are fixed or explicitly overruled with a concrete technical reason.
