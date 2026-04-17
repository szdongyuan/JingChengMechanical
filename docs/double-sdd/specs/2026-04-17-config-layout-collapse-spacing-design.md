# Config Layout Collapse Spacing Design

## Summary

The per-channel configuration editor in the config page can show unstable visual spacing after repeated expand/collapse actions on sections such as `STFT 参数` and `SPL 参数`. The apparent gap drift is not caused by the saved collapse state. It comes from the channel-detail vertical layout redistributing extra height into remaining `CollapsibleGroupBox` sections after earlier sections are collapsed.

This pass keeps the current config-page behavior and structure intact, and fixes only the layout policy so extra vertical space stays at the bottom of the channel editor instead of stretching section cards.

## Scope

In scope:

- stabilizing section layout in the config page channel-detail editor
- fixing repeated expand/collapse behavior for per-channel sections
- adding focused regression coverage for the layout behavior
- allowing only the minimum UI geometry changes required to eliminate the bug

Out of scope:

- changing section order, field order, or default expanded state
- changing config persistence, collapse-state persistence, or validation rules
- changing other modules that do not participate in the config-page layout bug
- redesigning `CollapsibleGroupBox` for global reuse outside the config page

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces:
  - config page field set and ordering
  - per-channel collapse/expand interaction
  - default expanded state of channel sections
  - config persistence and reload behavior
  - scroll-area overflow behavior when channel content is taller than the viewport
- Allowed breakage:
  - minor geometry differences that are strictly required to keep unused vertical space at the bottom of the channel editor
- Migration strategy: none

## Approved Decisions

1. Keep the fix scoped to the config page, with at most closely related config-page layout adjustments if required by the repair.
2. Preserve current user-visible behavior unless a geometry change is necessary to remove the bug.
3. Do not change data structures, saved config schema, or collapse-state persistence.
4. Prefer a layout-level fix in the channel editor over changing `CollapsibleGroupBox` globally.
5. Add regression coverage that makes the intermittent bug reproducible through deterministic geometry assertions.

## Root Cause

`ConfigModule._build_channel_tab()` builds each per-channel editor as a `QVBoxLayout` containing multiple `CollapsibleGroupBox` instances.

When the available viewport height is larger than the collapsed content height:

- collapsed sections shrink correctly
- the parent channel container keeps extra height
- the layout redistributes that extra height across remaining section widgets
- some expanded sections grow taller than their `sizeHint()`

This makes the UI look as though spacing between sections is drifting, even though the actual inter-widget layout spacing stays constant.

## Design Summary

The fix will keep the current section widgets and collapse logic, but it will change the per-channel container layout so unused vertical space is absorbed after the final section rather than being distributed into section widgets.

Mandatory implementation decision:

- update the channel-detail layout created in `ui/modules/config.py`
- add a bottom stretch to the per-channel `QVBoxLayout` built in `ConfigModule._build_channel_tab()`
- keep section widgets at their natural heights during expand/collapse cycles
- ensure repeated toggling of `STFT 参数` and `SPL 参数` does not stretch unaffected sections such as `FFT 频带上下限` or `1/3 倍频程参数`
- do not change `CollapsibleGroupBox` global default behavior for this pass

This keeps the repair local to the config page and avoids changing the default behavior of `CollapsibleGroupBox` in other contexts.

Overflow protection requirements:

- when the viewport is shorter than the full expanded channel content, the detail page must continue to rely on the existing `QScrollArea` scrolling behavior
- the layout fix must not introduce clipping, hidden controls, or missing scroll reachability in the channel-detail editor
- the bottom stretch may absorb extra space only when the viewport is taller than the laid-out content

## Testing Strategy

Add focused UI regression coverage in `tests/test_config_module.py` that:

- renders the config module through the normal visible path (`loaded_configs` + `module.show()` + `qapp.processEvents()`)
- resizes the module to a tall viewport so the collapsed-content case has excess vertical space
- records the baseline `sizeHint().height()` for unaffected expanded sections such as `fft` and `octave`
- repeatedly toggles the `stft` and `spl` sections through collapse/expand cycles with `qapp.processEvents()` between state changes
- asserts that unaffected expanded sections still have `height() == sizeHint().height()` after the cycles
- asserts that collapsed sections shrink to their collapsed `sizeHint()`

Add a second focused assertion path that:

- keeps the module at a shorter viewport where scrolling is required
- verifies the channel-detail area remains scrollable and the layout fix does not hide the final sections or make the scroll area unreachable

Existing config-module tests for collapse-state persistence and save/reload behavior should remain unchanged and continue to pass.

## Files Likely Affected

- `ui/modules/config.py`
- `tests/test_config_module.py`
