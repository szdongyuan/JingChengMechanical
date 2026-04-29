# Control Panel Status Row Removal Design

## Context

The control panel currently renders a status label between the start/stop button row and the manual pass/fail button row. This label shows controller-owned busy or blocking text such as `当前测试运行中`.

The requested UI change removes that visible status row entirely. To keep the panel visually balanced after the row is removed, the button stack should become slightly more compact vertically while each button becomes slightly taller.

## Goals

- Remove the visible status-text row from the control panel.
- Keep the control buttons and their enable/disable behavior unchanged.
- Reduce the control-module top-level vertical spacing from `10` to `8`.
- Set all four control buttons to a minimum height of `36` pixels so the panel still looks proportionate after the status row is gone.

## Non-Goals

- No changes to factory controller state transitions.
- No changes to start/stop/pass/fail button behavior.
- No changes to blocked-start modal behavior.
- No changes to other modules' state ownership.

## Design

### UI Structure

`ui/modules/control.py` will no longer create or add `lbl_start_reason` to the layout. The control panel will render only the two button rows.

The module `QVBoxLayout` spacing will be set to `8` instead of the current `10` so the gap between the two button rows becomes modestly tighter.

All four control buttons will receive `setMinimumHeight(36)` so the panel still feels proportionate after the status row is removed.

### Runtime Behavior

The existing busy-state and idle-blocking logic remains in place for button enabled/disabled state and blocked-start modal updates.

`_sync_start_state()` will continue to update button enabled state, but it will no longer render inline text for the removed status row.

### Tests

`tests/test_control_module.py` and any directly affected main-window coverage will be updated to:

- assert that `ControlModule` no longer exposes `lbl_start_reason`
- preserve coverage for start-button enabled/disabled behavior
- preserve coverage for blocked-start modal behavior
- add focused assertions that the control-module top-level layout spacing is `8`
- add focused assertions that `btn_start`, `btn_stop`, `btn_pass`, and `btn_fail` each have minimum height `36`
- replace the existing main-window assertion on `window.control.lbl_start_reason.text()` with assertions that the start button remains disabled in busy state and that `lbl_start_reason` is absent

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces: control panel button behavior, start-button disable rules, blocked-start modal behavior, manual action button availability
- Allowed breakage: removal of the visible inline status row; removal of the internal `ControlModule.lbl_start_reason` widget attribute; updated control-panel spacing and button height
- Migration strategy: none

## Verification

- Run `pytest tests/test_control_module.py -v`
- Run `pytest tests/test_main_window.py -v`
- Confirm the control panel shows only two button rows, no inline status label, and the focused tests still pass
