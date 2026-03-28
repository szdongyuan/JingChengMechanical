# Factory Config Priority Selection Design

## Context

The current factory workorder UI models selection as three separate controls:

- `Config A`
- `Config B`
- `Manual Primary`

That model no longer matches the intended operator workflow.

During design review, the operator clarified these requirements:

- factory config selection should apply immediately without an `应用` step
- the `应用` button should be removed
- `Manual Primary` should be removed
- users should be able to enable more than two configs
- users should be able to drag configs to set priority
- the highest-priority enabled config is the primary config
- the app should remember the last enabled configs and their priority order across restarts
- `SN` and `Model` should not be restored
- invalid restored configs should surface as an explicit blocking warning: `配置已失效`
- `自动开始` should remain clickable when selection or validation fails so the app can give strong feedback on click

This spec replaces the current two-config-plus-manual-primary workorder selection model with an ordered multi-config factory selection model.

## Scope

This spec covers:

- the factory config selection UI on the workorder page
- selection persistence across app restarts
- the controller contract for ordered enabled configs
- manual-start and trigger-start config resolution under ordered priority
- operator-facing feedback for invalid restored selections and blocked start attempts

In scope:

- replace `Config A`, `Config B`, and `Manual Primary` with a single ordered selection surface
- remove `应用`
- persist enabled config IDs and their order across restarts
- allow one or more enabled configs
- define priority-based start routing
- define blocking and non-blocking startup and start-click feedback

Out of scope:

- recorder, hardware, or buffer implementation changes
- STFT / SPL / FFT / octave rendering
- analysis result payload semantics
- PLC protocol changes
- changing config-definition persistence in the main SQLite schema

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - operator-facing factory start workflow
  - existing config definitions stored by `ConfigService`
  - existing runtime validation done by `ConfigService.validate_factory_runtime(...)`
  - existing controller phase model for in-progress states
- Allowed breakage:
  - remove the `应用` button
  - remove the `Manual Primary` selector
  - remove the current `Config A` / `Config B` selection contract
  - change manual start feedback from disabled-only to clickable-with-modal for validation failures
- Migration strategy: none

This is a direct workflow replacement. No compatibility mode is required for the old selection model.

## Approved Decisions

1. The workorder page replaces `Config A`, `Config B`, and `Manual Primary` with one ordered config list.
2. Each config row has a checkbox to enable or disable that config for factory mode.
3. Enabled configs can be reordered by drag and drop.
4. The highest-priority enabled config is the primary config.
5. Any number of configs may be enabled.
6. Manual `自动开始` always starts the highest-priority enabled config.
7. If multiple enabled configs match the same PLC or scan trigger, the app starts the highest-priority matching config.
8. If no enabled config matches a PLC or scan trigger, ignore the trigger for now.
9. `应用` is removed.
10. `Manual Primary` is removed.
11. Selection changes apply immediately.
12. The app remembers the latest enabled config IDs and their order across restarts.
13. `SN` and `Model` are not restored across restarts.
14. Restored missing configs are pruned from the remembered selection instead of creating fake placeholder rows.
15. If any remembered config was pruned during restore, show `配置已失效`.
16. `配置已失效` is a blocking warning.
17. `配置已失效` remains blocking until the operator makes any selection change in the factory config list; that first user edit clears the warning and overwrites the saved startup selection.
18. Checked configs render as a priority block at the top of the list; unchecked configs render below them in the base order returned by `ConfigService.list_configs()`.
19. Newly checked configs are appended to the end of the checked priority block until the operator drags them elsewhere.
20. Every ordinary list edit persists the ordered enabled config IDs immediately.
21. All enabled configs must pass startup/runtime validation before manual start is allowed to proceed.
22. The same blocking rules apply to PLC-triggered and scan-triggered starts.
23. If a PLC-triggered or scan-triggered start is blocked by selection or validation, the app must explicitly report that reason to the user rather than failing silently.
24. External trigger failures should use the same feedback principle as manual start: persistent inline warning plus modal popup with the blocking reason.
25. Blocking reason precedence is:
    - `配置已失效`
    - `配置加载失败`
    - `请至少启用一个配置`
    - first runtime validation error from the highest-priority enabled config that has any validation error
    - normal in-progress phase reasons
26. Manual `自动开始` remains clickable when selection or validation fails.
27. Clicking `自动开始` while blocked by selection or validation failure must show a modal error in addition to persistent inline warning text.
28. If a blocked-start modal is already open, additional blocked manual, PLC, or scan start attempts must not open another modal until the current modal is dismissed.
29. External triggers received during in-progress states keep the existing ignore-while-busy behavior and do not raise modal feedback.
30. The factory config list is locked during in-progress states and pending start.
31. `自动开始` is disabled only for true in-progress states such as:
    - `running`
    - `analyzing`
    - `reanalyzing`
    - pending start request
32. `配置已失效` is the single blocking warning for any automatic prune of selected configs, whether the prune happens during startup restore or later while the app is running.
33. Automatic prune during startup restore does not overwrite the saved startup selection until the operator makes the first list edit after restore.
34. Automatic prune during later `config_changed` handling updates the live selection immediately and attempts to persist the pruned ordered selection immediately, but the blocking `配置已失效` warning still remains until the operator makes a later selection change.
35. If persisting the ordered enabled config IDs to app settings fails, keep the live selection active for the current session, keep the last successfully saved selection for the next restart, and surface explicit non-blocking `配置保存失败` feedback.
36. `配置保存失败` clears automatically after the next successful persistence write.
37. Scan-trigger eligibility continues to use the existing per-config `scan_triggers_test` flag; ordered multi-config selection only changes which eligible config wins by priority.

## Design Summary

Replace the current pair-plus-primary model with one ordered selection list owned by the workorder module.

The ordered list becomes the single source of truth for factory selection:

- checked rows define which configs participate in factory mode
- row order defines priority
- index `0` in the enabled order is the primary config

The controller keeps using `selected_config_ids`, but it now interprets that tuple as an ordered list of enabled configs instead of a fixed pair that needs a separate `manual_primary_config_id`.

Persistence is lightweight UI state, so it should be stored in app settings rather than the main config database.

## Workorder UI

### Factory Config List

The workorder page should render one config list for factory selection.

Required behavior:

- every config row shows:
  - config name
  - enabled checkbox
  - optional compact secondary summary text if helpful
- checked rows render together as a priority block at the top of the list
- unchecked rows render below the checked block in the base order returned by `ConfigService.list_configs()`
- enabled rows may be dragged to change order
- unchecked rows remain available in the list but are ignored for factory selection
- checked rows define the enabled ordered selection
- the top checked row is visually identifiable as the primary config

The UI should not require a separate explicit apply step.

### Removed Controls

Remove these controls from the workorder page:

- `应用`
- `Manual Primary`
- the dedicated `Config A` / `Config B` combo-box pair

The selection model is replaced, not supplemented.

### Immediate Apply

Any user action that changes the enabled ordered selection applies immediately:

- checking a config
- unchecking a config
- dragging a checked config to a new position

Each of these actions updates the controller selection immediately and refreshes the workorder payload immediately.

The replacement workorder payload shape is:

- `sn: str`
- `model: str`
- `selected_config_ids: tuple[int, ...]`

When a user newly checks a config, that config is appended to the end of the checked priority block. The user may then drag it higher if a different priority is desired.

Each ordinary list edit is also persisted immediately to app settings:

- check
- uncheck
- drag reorder

The factory config list is editable only while the controller is idle. During `running`, `analyzing`, `reanalyzing`, and pending start, the list is locked.

If persisting an ordinary list edit to app settings fails:

- keep the edited enabled order active in the live UI and controller for the current session
- do not revert the operator's list edit
- keep the last successfully saved ordered enabled config IDs as the startup restore source for the next restart
- show explicit non-blocking `配置保存失败` feedback immediately
- clear `配置保存失败` automatically after the next successful persistence write

## Persistence

Persist only factory selection UI state:

- ordered enabled config IDs

Do not persist:

- `SN`
- `Model`

Persistence location:

- app settings, not the main SQLite config schema

### Startup Restore

On app startup:

1. load the remembered ordered enabled config IDs
2. remove any config IDs that no longer exist
3. restore the remaining existing enabled configs in remembered order
4. place all unchecked configs below the restored checked block in the base order returned by `ConfigService.list_configs()`
5. immediately apply the restored selection to the controller
6. if any remembered config was removed during restore, surface `配置已失效`

`配置已失效` is blocking. The operator must acknowledge the automatically pruned selection with a later list edit before start can proceed.

Startup restore must not overwrite the remembered startup selection merely because it pruned missing configs for the live UI state. The saved startup selection is overwritten only on the first user edit after restore acknowledges the invalid-selection warning.

The first user edit to the config list clears `配置已失效` and immediately overwrites the saved startup selection with the new ordered enabled config IDs.

The app must not create fake or synthetic list rows for deleted configs.

## Controller Contract

### Selection Model

`FactoryControllerState.selected_config_ids` remains the selection field, but its meaning changes:

- it is an ordered tuple of enabled config IDs
- the first element is the primary config
- later elements are lower-priority enabled configs

`manual_primary_config_id` is removed from the controller contract.

### Readiness

Selection readiness rules change:

- zero enabled configs: not ready
- one enabled config: allowed
- multiple enabled configs: allowed

However, start readiness still requires all enabled configs to pass runtime validation.

If any enabled config fails validation, start is blocked and the blocking reason should be explicit.

### Config Loading and Validation

When the controller refreshes selection:

- load every enabled config definition
- maintain the same ordered selection
- validate every enabled config with the existing runtime validation service

Start readiness passes only when:

- at least one config is enabled
- every enabled config loads successfully
- every enabled config passes runtime validation
- no in-progress controller state blocks start

When multiple blocking conditions coexist, the user-facing blocking reason must use this precedence:

1. `配置已失效`
2. `配置加载失败`
3. `请至少启用一个配置`
4. first runtime validation error from the highest-priority enabled config that has any validation error
5. normal in-progress phase reason

For idle states, inline feedback and modal feedback must show the same blocking reason.

During in-progress states, the inline feedback remains phase-owned and shows the in-progress reason. Any load/selection/validation changes discovered while busy are re-evaluated and surfaced when the controller returns to an idle state.

If an enabled config ID still exists in `ConfigService.list_configs()` but `load_config(config_id)` fails while refreshing selection, the user-facing blocking reason is `配置加载失败`.

When `config_changed` fires, the controller must re-run selection refresh for the current `selected_config_ids`. If the underlying config is now loadable and valid, `配置加载失败` clears without requiring the operator to change the selection list.

If `config_changed` reveals that a currently selected enabled config has been deleted, the controller must immediately prune that config from `selected_config_ids` and attempt to persist the new ordered selection. If the controller is idle, it surfaces blocking `配置已失效` immediately. If the controller is in an in-progress state, the prune/persist still happens immediately but the blocking warning is deferred until the controller returns to idle.

Runtime prune uses the same `配置已失效` warning state as startup restore:

- it blocks start until the operator makes any later selection change in the factory config list
- that later user edit clears `配置已失效`
- unlike startup restore, runtime prune does not wait for that later user edit before attempting to persist the pruned ordered selection
- if the persistence write fails, keep the live pruned selection, keep the last successfully saved selection unchanged for restart, and surface explicit non-blocking `配置保存失败`

For runtime validation, the authoritative first error for a config is `validate_factory_runtime(config)[0]`. The controller and tests must not reorder that list.

## Start Resolution

### Manual Start

When the operator clicks `自动开始` and the selection is valid:

- start the highest-priority enabled config
- pass that config as the active config for the run

### PLC Trigger

When a PLC trigger arrives:

- if the controller is already in an in-progress state, ignore the trigger without opening a modal
- otherwise, if start is currently blocked by any idle blocking reason, do not start a run, surface that blocking reason explicitly using the same inline-plus-modal feedback principle as manual start, and stop evaluation before any trigger matching
- find all enabled configs whose PLC mapping matches
- if exactly one matches, start it
- if multiple match, start the highest-priority matching config
- if none match, ignore the trigger

### Scan Trigger

When a scan trigger arrives:

- if the controller is already in an in-progress state, ignore the trigger without opening a modal
- otherwise, if start is currently blocked by any idle blocking reason, do not start a run, surface that blocking reason explicitly using the same inline-plus-modal feedback principle as manual start, and stop evaluation before any trigger matching
- find all enabled configs whose current config definition has `scan_triggers_test` enabled
- if exactly one matches, start it
- if multiple match, start the highest-priority matching config
- if none match, ignore the trigger

This continues the existing scan-trigger eligibility contract. Ordered multi-config selection does not add any extra scan-text matching rule beyond reusing the current `scan_triggers_test` predicate and then applying enabled-config priority.

## Operator Feedback

### Persistent Inline Feedback

The control area should continue to show persistent inline state near `自动开始`.

Required behavior:

- show clear blocking text whenever start cannot proceed
- show `配置已失效` when startup restore pruned one or more remembered configs
- keep the blocking message visible until the operator resolves it or a new blocking reason replaces it

### Click-Time Modal Feedback

`自动开始` should remain clickable for all idle blocking reasons, including:

- `配置已失效`
- `配置加载失败`
- `请至少启用一个配置`
- runtime validation failures

On click, if start is blocked by any idle blocking reason:

- do not start a run
- show a modal popup with the same blocking reason
- preserve the inline warning

This is required because disabled-only feedback is too silent for operators.

The same feedback principle applies to blocked PLC-triggered and scan-triggered starts when the app is otherwise idle. It does not override the existing ignore-while-busy behavior for in-progress states.

If a blocked-start modal is already open, additional blocked start attempts must not open another modal until the current modal is dismissed. If a later blocked attempt has a different blocking reason, the currently open modal must update in place to the newest blocking reason instead of staying stale or stacking another popup.

If background state refresh changes the highest-precedence idle blocking reason while a blocking modal is already open, the modal must update in place immediately to the newest current blocking reason. If the currently open modal's blocking reason becomes false and no other idle blocking reason replaces it, the modal should close automatically.

### Persistence Failure Feedback

If saving the ordered enabled config IDs to app settings fails:

- keep the live selection unchanged for the current session
- show explicit non-blocking `配置保存失败` feedback immediately
- do not treat `配置保存失败` as a start-blocking reason by itself
- keep using the last successfully saved selection on the next restart
- clear `配置保存失败` after the next successful persistence write

### Disabled Button States

`自动开始` should be disabled only for states where clicking is not meaningful:

- `running`
- `analyzing`
- `reanalyzing`
- pending start request

It should not be disabled merely because selection or validation is currently failing.

## UI State Model

### Normal Idle / Ready

- one or more configs checked
- all enabled configs valid
- no invalid restored config warning
- `自动开始` clickable and allowed to proceed

### Blocking Validation Failure

- at least one enabled config exists
- one or more enabled configs fail runtime validation
- inline warning visible
- `自动开始` remains clickable
- clicking `自动开始` shows a modal and does not start

### Blocking Load Failure

- at least one enabled config exists
- one or more enabled configs cannot be loaded even though their IDs still exist in `ConfigService.list_configs()`
- inline warning shows `配置加载失败`
- `自动开始` remains clickable
- clicking `自动开始` shows a modal and does not start

### Blocking Zero Selection

- no configs are enabled
- inline warning shows `请至少启用一个配置`
- `自动开始` remains clickable
- clicking `自动开始` shows a modal and does not start

### Blocking Invalid Selected Config

- one or more selected configs were pruned automatically, either during startup restore or later runtime `config_changed` handling
- inline warning shows `配置已失效`
- start remains blocked until the operator makes any later selection change in the factory config list
- `自动开始` remains clickable
- clicking `自动开始` shows a modal and does not start

### In-Progress

- controller in `running`, `analyzing`, `reanalyzing`, or pending start state
- factory config list locked
- `自动开始` disabled
- in-progress reason shown inline using the existing controller-phase behavior
- background config/load/validation refreshes do not replace the inline busy reason until the controller returns to idle

## Testing Requirements

Add or update tests for:

- ordered checklist rendering and immediate apply behavior
- check/uncheck changes updating controller selection immediately
- drag reorder updating controller selection order immediately
- top checked config treated as primary config
- one checked config is selection-ready
- any number of checked configs is allowed
- startup restore of checked config IDs and order
- ordinary list edits persist immediately to app settings
- persistence write failure keeps the live selection active, surfaces non-blocking `配置保存失败`, and preserves the last successfully saved selection for restart
- unchecked configs reconstruct below the restored checked block in `ConfigService.list_configs()` order
- missing restored config IDs are pruned
- pruned restore surfaces blocking `配置已失效`
- startup restore does not persist the pruned result until the first user edit acknowledges it
- first user edit after `配置已失效` clears the warning and overwrites saved startup selection
- blocking reason precedence is stable across inline and modal feedback
- replacement workorder payload is `sn`, `model`, and ordered `selected_config_ids`
- config-load failure surfaces as `配置加载失败`
- `config_changed` refresh clears `配置加载失败` once loading succeeds again
- `config_changed` pruning of deleted selected configs persists the new selection and surfaces `配置已失效`
- runtime-pruned `配置已失效` remains blocking until a later user list edit clears it
- zero enabled configs surface as `请至少启用一个配置`
- validation error selection is deterministic by enabled priority, then validator error order
- factory config list locks during in-progress states
- manual start chooses the top checked config
- PLC trigger chooses the highest-priority matching config
- scan trigger uses the existing `scan_triggers_test` eligibility rule and chooses the highest-priority eligible config
- all enabled configs must pass validation or start is blocked
- blocked manual start keeps `自动开始` clickable and shows modal feedback
- blocked PLC-triggered start surfaces inline and modal feedback
- blocked scan-triggered start surfaces inline and modal feedback
- in-progress external triggers are ignored without modal popup
- repeated blocked start attempts do not stack modal popups and update the open modal in place when the blocking reason changes
- a blocking modal closes automatically once its current reason is no longer true
- in-progress phases still disable `自动开始`

## Non-Goals

This spec does not define:

- a new database schema for remembered selection
- cross-machine syncing of remembered startup state
- special handling for direct DB tampering beyond pruning missing configs on startup
- new trigger-routing semantics beyond highest-priority matching selection
