# Manual Judgment Persistence And Source Design

Date: 2026-05-08

## Problem

Factory-mode manual OK/NG buttons currently only emit a log message. They do not update the home-page analysis UI, they do not persist a final judgment to the database, and they do not move the page into the ready state for the next unit.

The product requirement is:

- One test corresponds to one audio data record.
- Automatic analysis writes the initial result.
- Manual OK/NG may overwrite that same record as the final judgment.
- The database and data-management page must show whether the final judgment came from automatic analysis or manual judgment.
- After manual OK/NG succeeds, the page should return to the no-active-test ready state and clear per-unit inputs/display.

## Current Context

Relevant existing behavior:

- `ControlModule` wires `btn_pass` and `btn_fail` to log-only lambdas.
- `TestSession._save_wav()` inserts one `AudioDataTable` record for a saved recording.
- `TestSession._persist_audio_result_metadata()` updates `AudioDataTable.labels` to `OK` or `NG` after automatic analysis.
- `AudioDataTable` currently contains `labels`, `barcode`, `analyse_config`, and `config_content`, but no source field.
- `DataManagementModule` displays `AudioDataService.list_column_names()` output and filters/export data by `labels`.
- `FactoryModeController.clear_display_state(reason)` delegates to `clear_for_next_run(reason)`, which emits `factory_display_reset`, an empty review payload, and an updated factory state when not busy.
- Result/STFT/chart modules already respond to `factory_display_reset` and `factory_review_changed`, but OK/NG does not currently emit either because no reset is requested.

## Goals

1. Persist manual OK/NG as the final judgment for the current test's existing audio data row.
2. Add a judgment source field so records can distinguish automatic and manual final judgments.
3. Display the judgment source in data management.
4. Keep one test mapped to one audio data record; manual judgment must update the existing row, not create a second record.
5. Allow manual OK/NG when the current test has a saved audio data row, even if automatic analysis failed or produced no result.
6. Reject manual OK/NG when there is no current saved audio data row or when the test is still active.
7. After successful manual OK/NG, clear the per-unit SN and analysis/result/chart/STFT display and return to the ready/no-active-test state.
8. Avoid legacy-data migration burden during development.

## Non-Goals

- Do not create a separate manual-judgment table.
- Do not preserve automatic labels separately from manual overrides.
- Do not implement audit history for label changes.
- Do not backfill old test records.
- Do not support old copied databases that lack the new column.
- Do not change export packaging semantics beyond including the new source column in the exported database when present in the active schema.

## Data Model

Add one field to `audio_data_table`:

- ORM name: `label_source`
- DB column name: `label_source`
- Type: text / nullable
- Values:
  - `auto`: final `labels` value came from automatic analysis.
  - `manual`: final `labels` value came from manual OK/NG.
  - `NULL` or empty: no source recorded. Old development/test rows may have this value.

No migration or backfill is required. The final application database should already include this column.

## Persistence Rules

### Automatic Analysis

When automatic analysis successfully produces a judgment:

- If the result overall is `合格`, update the saved audio data row with:
  - `labels = "OK"`
  - `label_source = "auto"`
- If the result overall is `不合格`, update the saved audio data row with:
  - `labels = "NG"`
  - `label_source = "auto"`
- Keep existing config metadata persistence:
  - `analyse_config`
  - `config_content`

If automatic analysis has no saved audio data row, keep the existing warning behavior and do not create a row.

### Manual Judgment

When the operator clicks manual OK:

- Update the current test's saved audio data row with:
  - `labels = "OK"`
  - `label_source = "manual"`

When the operator clicks manual NG:

- Update the current test's saved audio data row with:
  - `labels = "NG"`
  - `label_source = "manual"`

Manual judgment overwrites the row's current `labels` and `label_source`. It does not create another row.

## Manual Judgment Eligibility

Manual OK/NG is allowed only when all of the following are true:

1. The factory controller is not in a busy phase:
   - not pending start
   - not `running`
   - not `analyzing`
   - not `reanalyzing`
2. There is a current displayed/review/stopped test context that maps to a backend session.
3. That session has successfully saved an audio data row and has a non-empty audio data id.
4. The audio data row still exists in the database.

Manual OK/NG is allowed for these saved-row cases:

- automatic analysis succeeded
- automatic analysis failed/no-result after the recording was saved
- user stopped early but partial WAV save succeeded

Manual OK/NG is rejected for these cases:

- initial state or already reset state
- startup failure before recording
- active recording/analysis/reanalysis
- no current backend session for the displayed config/run
- no saved audio data id
- saved audio data id points to no database row
- database update fails

Rejected manual judgment must not clear the page or change labels. It should provide visible operator feedback, not only a log line.

## UI Behavior

### Control Panel

Replace the current OK/NG log-only behavior with a manual judgment action:

1. Request the controller/backend to persist the manual judgment.
2. On success:
   - keep a clear log entry for the action
   - reset the factory display state using the existing display reset contract
   - clear per-unit SN and stale analysis/result/chart/STFT display data
3. On failure:
   - show a modal warning with the failure reason
   - keep the current page state unchanged

Manual OK/NG should not force-reset busy tests.

### Data Management

Display `label_source` as a normal table column:

- Header text: `判定来源`
- `auto` renders as `自动`
- `manual` renders as `人工`
- null/empty renders as blank
- Unknown source values render as their raw text so bad data is visible during development.

Filtering by OK/NG remains based on `labels`. No source filter is required in this pass.

### Per-Unit Reset After Successful Manual Judgment

On successful manual OK/NG:

- Clear the workorder SN field.
- Preserve product model.
- Preserve selected factory configs.
- Publish/cache the updated workorder payload with blank SN.
- Clear analysis result/chart/STFT state through `factory_display_reset`.
- Keep the live camera preview unchanged. Manual judgment reset must not clear, stop, start, refresh, or otherwise disturb `VideoModule`.
- Return the factory controller to `armed` if selected configs are ready, otherwise `not_ready`.

## Component Responsibilities

### `TestSession`

Owns audio-data-row persistence for a session:

- Store/expose the current `_audio_data_id` for the saved row.
- Continue writing automatic `labels`.
- Also write `label_source = "auto"` for automatic result persistence.
- Provide or support a method for manual label update on the session's saved row.

### `TestManager`

Owns session lookup and manual judgment persistence:

- Provide a factory-mode callable for manual judgment, for example `apply_manual_judgment(config_id, run_id, label)`.
- Resolve the current session from active or completed sessions.
- Verify run ownership when a run id is available, so stale UI state cannot label the wrong session.
- Return a success/failure result with an operator-facing message.

### `FactoryModeController`

Owns page state and reset coordination:

- Expose a manual judgment entry point, for example `apply_manual_judgment(label)`.
- Reject busy states before calling the backend.
- Use current `displayed_config_id` and review/run context to target the backend call.
- On backend success, call `clear_for_next_run("manual_judgment_ok")` or `clear_for_next_run("manual_judgment_ng")`.
- On backend failure, emit or return a failure reason for the UI.

### `ControlModule`

Owns button click feedback:

- Wire OK to the controller manual-judgment method with `OK`.
- Wire NG to the controller manual-judgment method with `NG`.
- Show a warning dialog when the controller reports failure.
- Do not duplicate database logic in the UI module.

### `WorkOrderModule`

Responds to successful manual judgment display resets:

- Subscribe to `factory_display_reset`.
- For manual judgment reset reasons, clear only SN and publish/cache the blank-SN payload.
- Do not clear product model or selected configs.

### `VideoModule`

Owns only the live camera preview:

- Do not subscribe to `factory_display_reset`.
- Do not clear the preview pixmap or show the empty placeholder because of manual judgment reset.
- Do not start, stop, reopen, or refresh cameras as part of manual judgment reset.

## Compatibility / Migration

- Backward compatibility: not required.
- Protected surfaces:
  - New database schema for final copied application database.
  - User-visible data-management table behavior.
  - Factory-mode one-test-one-record behavior.
- Allowed breakage:
  - Old development/test databases that lack `audio_data_table.label_source` may fail until replaced by a new copied database.
  - Old records may show blank source.
- Migration strategy: none.
- Rationale: the software is still in development and existing records are test-only; adding migration/backfill would add final-product burden without value.

## Testing Requirements

Add focused tests for:

1. Automatic result persistence writes `labels` and `label_source = "auto"`.
2. Manual OK updates the current saved row to `labels = "OK"` and `label_source = "manual"`.
3. Manual NG updates the current saved row to `labels = "NG"` and `label_source = "manual"`.
4. Manual judgment can update a saved row after automatic no-result/failure.
5. Manual judgment can update a partial saved row after early stop.
6. Manual judgment is rejected with visible feedback when there is no saved row/current session.
7. Manual judgment is rejected in busy phases and does not clear display.
8. Successful manual judgment clears SN, clears analysis/result/chart/STFT display, returns to ready/not-ready through existing reset signals, and leaves the live camera preview untouched.
9. Data management displays `label_source` as `判定来源`, mapping `auto` to `自动`, `manual` to `人工`, blank to blank.

## Verification

Targeted commands:

- `pytest tests/test_control_module.py`
- `pytest tests/test_workorder_module.py`
- `pytest tests/test_test_manager tests/test_test_session.py`
- `pytest tests/test_data_management_module.py tests/test_audio_data_service.py`

Final verification:

- `pytest`
