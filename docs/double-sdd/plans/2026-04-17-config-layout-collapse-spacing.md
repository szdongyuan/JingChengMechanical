# Config Layout Collapse Spacing Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the config page’s per-channel collapsible sections at stable natural heights during repeated expand/collapse actions, while preserving scroll behavior when the viewport is short.

**Architecture:** Keep the fix local to `ConfigModule` by changing the per-channel `QVBoxLayout` built in `_build_channel_tab()` so excess vertical space is absorbed below the final section instead of stretching `CollapsibleGroupBox` widgets. Lock the behavior with deterministic config-module geometry tests for both the tall-viewport stretch case and the short-viewport scroll case.
Add a minimal geometry-refresh update in `CollapsibleGroupBox.set_expanded()` so later collapsed sections publish their new `sizeHint()` immediately after visibility changes.

**Tech Stack:** Python 3.12, PyQt6, pytest

---

## File Structure

- Modify: `ui/modules/config.py`
  - Add the bottom stretch to the per-channel channel-detail layout in `ConfigModule._build_channel_tab()`.
  - Keep all existing section order, default expanded state, and collapse-state behavior unchanged.
- Modify: `ui/widgets/collapsible_group_box.py`
  - Invalidate widget geometry immediately after content visibility changes so collapsed sections publish their new natural height to parent layouts.
- Modify: `tests/test_config_module.py`
  - Add a focused tall-viewport regression that reproduces the section-stretch bug with repeated `stft` / `spl` toggles and asserts unaffected sections stay at `sizeHint().height()`.
  - Add a focused scroll regression that proves the existing `QScrollArea` can fully bring the final section into the viewport.
  - Add a focused save/reload geometry regression that proves restored collapsed sections still render at collapsed height in a tall viewport.

### Task 1: Stabilize Config Channel Section Layout

**Files:**
- Modify: `ui/modules/config.py`
- Modify: `ui/widgets/collapsible_group_box.py`
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
    module.resize(900, 700)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    scroll_bar = module.detail_scroll.verticalScrollBar()

    assert scroll_bar.maximum() > 0

    module.detail_scroll.ensureWidgetVisible(boxes["octave"], 0, 0)
    qapp.processEvents()

    top_in_viewport = boxes["octave"].mapTo(
        module.detail_scroll.viewport(),
        QPoint(0, 0),
    ).y()
    bottom_in_viewport = boxes["octave"].mapTo(
        module.detail_scroll.viewport(),
        QPoint(0, boxes["octave"].height()),
    ).y()

    assert top_in_viewport >= 0
    assert bottom_in_viewport <= module.detail_scroll.viewport().height()
```

```python
def test_on_save_preserves_collapsed_section_geometry_in_tall_viewport(
    qapp, monkeypatch
):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service, bus=bus)
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    boxes["stft"].set_expanded(False)
    qapp.processEvents()
    boxes["spl"].set_expanded(False)
    qapp.processEvents()

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr("ui.modules.config.QMessageBox.information", lambda *args: None)

    module._on_save()
    qapp.processEvents()

    restored = module._channel_containers[0]
    restored_boxes = {
        box.section_id(): box
        for box in restored.findChildren(CollapsibleGroupBox)
    }

    assert restored_boxes["stft"].is_expanded() is False
    assert restored_boxes["spl"].is_expanded() is False
    assert restored_boxes["stft"].height() == restored_boxes["stft"].sizeHint().height()
    assert restored_boxes["spl"].height() == restored_boxes["spl"].sizeHint().height()
    for key in ("loudness", "golden_sample", "fft", "octave"):
        assert restored_boxes[key].height() == restored_boxes[key].sizeHint().height()
```

- [ ] **Step 2: Run the focused regressions to verify the current implementation fails**

Run:

```powershell
pytest tests/test_config_module.py -q -k "channel_section_layout_does_not_stretch_after_repeated_collapses or channel_section_layout_preserves_scroll_reachability_in_short_viewport or on_save_preserves_collapsed_section_geometry_in_tall_viewport"
```

Expected: FAIL because the current channel-detail `QVBoxLayout` redistributes extra height into remaining expanded sections when `stft` and `spl` are collapsed in a tall viewport, and `CollapsibleGroupBox` does not yet publish the later collapsed section's updated geometry immediately after visibility changes.

- [ ] **Step 3: Implement the minimal layout fix**

Update `ui/modules/config.py` in `ConfigModule._build_channel_tab()`:

```python
    lay.addWidget(oct_section)
    lay.addStretch(1)

    self._apply_golden_mode_state(fft_mode, fft_overlay)
```

Update `ui/widgets/collapsible_group_box.py` in `set_expanded()`:

```python
    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self._content.setVisible(self._expanded)
        self.updateGeometry()
        arrow = "▼" if self._expanded else "►"
        self._header.setText(f"{arrow} {self._title}")
```

Implementation constraints:

- do not reorder sections
- do not change section titles or default expanded state
- keep the `CollapsibleGroupBox` change limited to geometry refresh only
- do not change persistence or save/reload behavior

- [ ] **Step 4: Run the focused regressions to verify the fix passes**

Run:

```powershell
pytest tests/test_config_module.py -q -k "channel_section_layout_does_not_stretch_after_repeated_collapses or channel_section_layout_preserves_scroll_reachability_in_short_viewport or on_save_preserves_collapsed_section_geometry_in_tall_viewport"
```

Expected: PASS with collapsed sections shrinking to their collapsed `sizeHint()`, unaffected sections staying at their natural heights, the rebuilt editor preserving that geometry after save/reload, and the scroll area able to bring the final section fully into view.

- [ ] **Step 5: Run the broader config-module regression coverage**

Run:

```powershell
pytest tests/test_config_module.py -q
```

Expected: PASS with the new layout tests green and no regressions to existing config save/reload, collapse-state, golden-sample, or AI-binding behaviors.

- [ ] **Step 6: Commit**

```bash
git add ui/modules/config.py ui/widgets/collapsible_group_box.py tests/test_config_module.py
git commit -m "fix(config): stabilize collapsed channel layout spacing"
```
