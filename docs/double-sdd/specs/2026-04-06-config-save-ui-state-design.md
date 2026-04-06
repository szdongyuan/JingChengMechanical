# Config Save UI State Design

## Summary

Saving a configuration currently rebuilds the entire config editor from the freshly reloaded authoritative record. That reload is correct for data integrity, but it also resets transient editor UI state: the selected channel tab falls back to the first tab and every per-channel collapsible section reopens in its default expanded state. In addition, successful saves do not provide reliable visible feedback because the current save path only emits `bus.status`, while the main window status bar is wired to `bus.log`.

This pass preserves the config editor's current save-time UI context across a successful authoritative reload and adds explicit success feedback that does not depend on bus wiring.

## Problem

Current successful save behavior in `ConfigModule._on_save()`:

- collect editor data
- validate and commit config + AI binding
- reload the saved config from storage
- rebuild the config list and detail editor from scratch

Observed issues:

- channel tab selection resets after save
- per-channel collapsible groups such as FFT/STFT/SPL are recreated in their default expanded state
- users can lose their current working context immediately after save
- save success is not clearly communicated to the operator

The bug is not the authoritative reload itself; the problem is losing editor-only UI state across that reload and relying on a weak success-notification path.

## Scope

In scope:

- preserving the selected channel tab across a successful save/reload when that channel still exists
- preserving each per-channel `CollapsibleGroupBox` expanded/collapsed state across a successful save/reload
- falling back safely when the previously selected tab or section set no longer exists
- adding explicit save-success feedback for successful config saves
- focused regression coverage for both preserved UI state and success feedback

Out of scope:

- changing config persistence format or validation rules
- changing save failure behavior
- changing hardware-change reload behavior
- preserving unrelated transient UI state such as scroll position or text selection
- changing analysis/config semantics
- changing non-config modules

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - authoritative reload after successful config save
  - saved config data and AI binding payloads
  - existing save failure warning behavior
  - channel data collection and validation behavior
  - channel/section widget structure and object names used by existing tests
  - stable per-section restore ids introduced for config editor collapsible sections in this pass
- Allowed breakage:
  - after a successful save, the editor no longer resets to the first channel tab and default-expanded sections when the previous UI state can be preserved
  - config save now shows explicit visible success feedback
- Migration strategy: none

## Approved Decisions

1. Keep the authoritative reload after successful save.
2. Preserve the currently selected channel by `channel_index` across the successful reload when that channel still exists; otherwise fall back to the first available channel tab.
3. Preserve each per-channel collapsible section's expanded/collapsed state across the successful reload using `(channel_index, section_id)` as the restore key for this pass.
4. Preserve UI state only for the successful save/reload path; failure paths and other reload paths stay unchanged in this pass.
5. Add explicit success feedback using `QMessageBox.information(...)` after the authoritative reload succeeds, rather than relying solely on `bus.status`.
6. Preserve the existing success bus notification contract exactly as `self.services.bus.status.emit("配置已保存")`, emitted only after the authoritative reload/UI rebuild succeeds and immediately before the success dialog is shown.
7. Add a small public state API to `CollapsibleGroupBox` instead of reading or mutating its private fields from `ConfigModule`.
8. The zero-channel post-save path remains part of the required behavior and must be covered by a concrete save-path regression.

## Design Summary

### UI state capture and restore

Before rebuilding the detail editor during a successful save:

- capture the currently selected channel by `channel_index`
- capture per-channel collapsible section state keyed by:
  - channel index
  - section id

`CollapsibleGroupBox` changes in this pass:

- accept a stable `section_id` at construction time
- expose a public `section_id()` reader
- expose a public `is_expanded()` reader
- expose a public `set_expanded(bool)` writer
- keep the current visual header/content behavior unchanged

`ConfigModule` will use only this public API when capturing/restoring section state.

For the config editor in this pass, the stable section ids are:

- `stft`
- `spl`
- `loudness`
- `golden_sample`
- `fft`
- `octave`

After the authoritative config is reloaded and `_populate_detail(...)` rebuilds the UI:

- treat the rebuilt editor's actual channel-tab set as the source of truth for channel availability in this save flow
- restore each matching section's expanded/collapsed state
- restore the previously selected channel tab whose container `channel_index` matches the captured value
- if the saved tab no longer exists, select the first available channel tab
- if the authoritative reload produces no available channels, keep the existing no-channel rendering path: no selected tab, channel host hidden, and warning state controlled by the current `_build_channel_tabs(...)` behavior

This preserves operator context without bypassing the authoritative reload.

### Success feedback

On the successful-save path only, after the authoritative reload and UI rebuild succeed:

- show `QMessageBox.information(...)` with an explicit config-save success message
- emit `self.services.bus.status.emit("配置已保存")` immediately before the success dialog so the current non-error integration remains intact

For this pass, `post-commit notification` means any post-persistence callback/notification that runs after data has already been committed but before `_on_save()` finishes, such as config-service notifications triggered during commit hooks.

If save, commit, post-commit notification, or reload fails:

- keep the existing warning/error behavior
- do not show the success dialog
- do not emit the success status message after that failure

## Rendering / Interaction Rules

After a successful config save:

- the same config remains selected
- the previously selected channel tab whose `channel_index` matches the pre-save selection remains selected when still present
- a collapsed section stays collapsed when the same `(channel_index, section_id)` exists after reload
- an expanded section stays expanded when the same `(channel_index, section_id)` exists after reload
- if the previously selected tab disappears because the rebuilt editor channel-tab set no longer contains that `channel_index`, the first available tab becomes selected
- if the rebuilt editor channel-tab set is empty after reload, no tab is selected and the existing no-channel state remains in effect
- the operator receives explicit visible success confirmation

After a failed config save:

- current failure handling remains unchanged in this pass

## Testing Strategy

Add focused config-module coverage for:

- a successful save preserving the selected channel tab
- a successful save preserving at least one collapsed section and one expanded section in a rebuilt channel editor
- explicit success feedback being shown only on successful save
- preservation of the exact success status signal/message contract on the successful path
- post-save fallback to the first available channel when the rebuilt editor channel-tab set no longer contains the previously selected `channel_index`
- post-save no-channel behavior when the rebuilt editor channel-tab set is empty after reload
- `CollapsibleGroupBox` public state API behavior needed by the save-state restore path
- save failure paths continuing to use warning behavior without success feedback

Verification should include:

- targeted `tests/test_config_module.py` cases covering the successful-save path
- targeted `tests` for the new `CollapsibleGroupBox` public state API if added outside existing config-module coverage
- the directly affected broader `tests/test_config_module.py` file

## Files Likely Affected

- `ui/widgets/collapsible_group_box.py`
- `ui/modules/config.py`
- `tests/test_config_module.py`

## Non-Goals

This design does not introduce:

- a toast/snackbar system
- scroll-position restoration
- generalized UI-state persistence across app restarts
- config save autosave
- refactors of the config editor layout
