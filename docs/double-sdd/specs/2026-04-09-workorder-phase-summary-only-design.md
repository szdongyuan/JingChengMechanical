# Workorder Phase Summary Only Design

## Context

The factory workorder input card currently renders two summary labels between the configuration list and the control buttons:

- `当前阶段：...`
- `最近触发方式：...`

The requested UI change removes the second label so that only the current phase remains visible in that space.

## Goals

- Keep a single status summary line in the workorder input card.
- Preserve the existing phase label mapping and update behavior.
- Remove the trigger summary line and its occupied vertical space.

## Non-Goals

- No changes to the factory controller state machine.
- No changes to trigger-source tracking in backend state.
- No changes to control button behavior or layout.

## Design

### UI Structure

`ui/modules/workorder.py` will no longer create or add `lbl_trigger_summary` to the layout. The module will keep `lbl_phase_summary` as the only visible status summary label in the workorder card.

### Runtime Behavior

`_update_phase_trigger_summary()` will be simplified so it updates only the phase summary text. Trigger-source values may still exist in `FactoryControllerState`, but the workorder module will not render them.

### Tests

`tests/test_workorder_module.py` will be updated to:

- assert that the workorder module no longer exposes `lbl_trigger_summary`
- assert that rendered workorder-card text no longer includes any `最近触发方式` summary line
- stop asserting on `lbl_trigger_summary`
- stop including trigger summary text in rendered-text helpers
- keep phase-summary coverage intact

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces: workorder card layout, current-phase summary text, existing factory-state update flow
- Allowed breakage: removal of the visible `最近触发方式` summary line from the workorder card; removal of the internal `WorkOrderModule.lbl_trigger_summary` widget attribute
- Migration strategy: none

## Verification

- Run the focused workorder module test file.
- Confirm the workorder card still renders the phase summary and no longer references the trigger summary label.
