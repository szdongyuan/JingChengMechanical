# Config Layout Collapse Spacing Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the config page’s per-channel collapsible sections at stable natural heights during repeated expand/collapse actions, while preserving scroll behavior when the viewport is short.

**Architecture:** Keep the fix local to `ConfigModule` by changing the per-channel `QVBoxLayout` built in `_build_channel_tab()` so excess vertical space is absorbed below the final section instead of stretching `CollapsibleGroupBox` widgets. Lock the behavior with deterministic config-module geometry tests for both the tall-viewport stretch case and the short-viewport scroll case.

**Tech Stack:** Python 3.12, PyQt6, pytest

---

## File Structure

- Modify: `ui/modules/config.py`
  - Add the bottom stretch to the per-channel channel-detail layout in `ConfigModule._build_channel_tab()`.
  - Keep all existing section order, default expanded state, and collapse-state behavior unchanged.
- Modify: `tests/test_config_module.py`
  - Add a focused tall-viewport regression that reproduces the section-stretch bug with repeated `stft` / `spl` toggles and asserts unaffected sections stay at `sizeHint().height()`.
  - Add a focused short-viewport regression that verifies the existing `QScrollArea` still allows the final channel section to remain reachable.

### Task 1: Stabilize Config Channel Section Layout

**Files:**
- Modify: `ui/modules/config.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write the failing layout regressions**

Add these focused tests to `tests/test_config_module.py` near the existing config UI-state regressions:

```python
from PyQt6.QtCore import QPoint


def test_channel_section_layout_does_not_stretch_after_repeated_collapses(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    baseline = {
        key: boxes[key].sizeHint().height()
        for key in ("loudness", "golden_sample", "fft", "octave")
    }

    for _ in range(5):
        boxes["stft"].set_expanded(False)
        qapp.processEvents()
        boxes["spl"].set_expanded(False)
        qapp.processEvents()
        boxes["stft"].set_expanded(True)
        qapp.processEvents()
        boxes["spl"].set_expanded(True)
        qapp.processEvents()

    boxes["stft"].set_expanded(False)
    qapp.processEvents()
    boxes["spl"].set_expanded(False)
    qapp.processEvents()

    assert boxes["stft"].height() == boxes["stft"].sizeHint().height()
    assert boxes["spl"].height() == boxes["spl"].sizeHint().height()
    for key, expected_height in baseline.items():
        assert boxes[key].height() == expected_height
        assert boxes[key].height() == boxes[key].sizeHint().height()
```

```python
def test_channel_section_layout_preserves_scroll_reachability_in_short_viewport(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(900, 420)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    scroll_bar = module.detail_scroll.verticalScrollBar()

    assert scroll_bar.maximum() > 0

    scroll_bar.setValue(scroll_bar.maximum())
    qapp.processEvents()

    last_box_bottom = boxes["octave"].mapTo(
        module.detail_scroll.widget(),
        QPoint(0, boxes["octave"].height()),
    ).y()
    reachable_bottom = scroll_bar.maximum() + module.detail_scroll.viewport().height()

    assert last_box_bottom <= reachable_bottom
```

- [ ] **Step 2: Run the focused regressions to verify the current implementation fails**

Run:

```powershell
pytest tests/test_config_module.py -q -k "channel_section_layout_does_not_stretch_after_repeated_collapses or channel_section_layout_preserves_scroll_reachability_in_short_viewport"
```

Expected: FAIL because the current channel-detail `QVBoxLayout` redistributes extra height into remaining expanded sections when `stft` and `spl` are collapsed in a tall viewport.

- [ ] **Step 3: Implement the minimal layout fix**

Update `ui/modules/config.py` in `ConfigModule._build_channel_tab()`:

```python
    lay.addWidget(oct_section)
    lay.addStretch(1)

    self._apply_golden_mode_state(fft_mode, fft_overlay)
```

Implementation constraints:

- do not reorder sections
- do not change section titles or default expanded state
- do not modify `CollapsibleGroupBox` in this pass
- do not change persistence or save/reload behavior

- [ ] **Step 4: Run the focused regressions to verify the fix passes**

Run:

```powershell
pytest tests/test_config_module.py -q -k "channel_section_layout_does_not_stretch_after_repeated_collapses or channel_section_layout_preserves_scroll_reachability_in_short_viewport"
```

Expected: PASS with collapsed sections shrinking to their collapsed `sizeHint()`, unaffected sections staying at their natural heights, and the short viewport still able to reach the final section through the existing scroll area.

- [ ] **Step 5: Run the broader config-module regression coverage**

Run:

```powershell
pytest tests/test_config_module.py -q
```

Expected: PASS with the new layout tests green and no regressions to existing config save/reload, collapse-state, golden-sample, or AI-binding behaviors.

- [ ] **Step 6: Commit**

```bash
git add ui/modules/config.py tests/test_config_module.py
git commit -m "fix(config): stabilize collapsed channel layout spacing"
```
