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
17. All enabled configs must pass startup/runtime validation before manual start is allowed to proceed.
18. `自动开始` remains clickable when selection or validation fails.
19. Clicking `自动开始` while blocked by selection or validation failure must show a modal error in addition to persistent inline warning text.
20. `自动开始` is disabled only for true in-progress states such as:
    - `running`
    - `analyzing`
    - `reanalyzing`
    - pending start request

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
4. immediately apply the restored selection to the controller
5. if any remembered config was removed during restore, surface `配置已失效`

`配置已失效` is blocking. The operator must resolve the selection before start can proceed.

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

## Start Resolution

### Manual Start

When the operator clicks `自动开始` and the selection is valid:

- start the highest-priority enabled config
- pass that config as the active config for the run

### PLC Trigger

When a PLC trigger arrives:

- find all enabled configs whose PLC mapping matches
- if exactly one matches, start it
- if multiple match, start the highest-priority matching config
- if none match, ignore the trigger

### Scan Trigger

When a scan trigger arrives:

- find all enabled configs eligible for scan-triggered start
- if exactly one matches, start it
- if multiple match, start the highest-priority matching config
- if none match, ignore the trigger

## Operator Feedback

### Persistent Inline Feedback

The control area should continue to show persistent inline state near `自动开始`.

Required behavior:

- show clear blocking text whenever start cannot proceed
- show `配置已失效` when startup restore pruned one or more remembered configs
- keep the blocking message visible until the operator resolves it or a new blocking reason replaces it

### Click-Time Modal Feedback

`自动开始` should remain clickable for selection and validation failures.

On click, if start is blocked by selection or validation:

- do not start a run
- show a modal popup with the same blocking reason
- preserve the inline warning

This is required because disabled-only feedback is too silent for operators.

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

### Blocking Restored Invalid Config

- startup restore pruned one or more missing configs
- inline warning shows `配置已失效`
- `自动开始` remains clickable
- clicking `自动开始` shows a modal and does not start

### In-Progress

- controller in `running`, `analyzing`, `reanalyzing`, or pending start state
- `自动开始` disabled
- in-progress reason shown inline using the existing controller-phase behavior

## Testing Requirements

Add or update tests for:

- ordered checklist rendering and immediate apply behavior
- check/uncheck changes updating controller selection immediately
- drag reorder updating controller selection order immediately
- top checked config treated as primary config
- one checked config is selection-ready
- any number of checked configs is allowed
- startup restore of checked config IDs and order
- missing restored config IDs are pruned
- pruned restore surfaces blocking `配置已失效`
- manual start chooses the top checked config
- PLC trigger chooses the highest-priority matching config
- scan trigger chooses the highest-priority matching config
- all enabled configs must pass validation or start is blocked
- blocked manual start keeps `自动开始` clickable and shows modal feedback
- in-progress phases still disable `自动开始`

## Non-Goals

This spec does not define:

- a new database schema for remembered selection
- cross-machine syncing of remembered startup state
- special handling for direct DB tampering beyond pruning missing configs on startup
- new trigger-routing semantics beyond highest-priority matching selection
