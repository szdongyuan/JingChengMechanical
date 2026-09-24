# Configurable Manual Judgment Buttons Design

## Summary

Add one per-test-configuration switch that controls whether the homepage result card exposes the complete manual judgment button group. The group is all-or-nothing: when enabled it shows the existing OK button together with all configured NG fault-type buttons; when disabled it is absent. The switch defaults to disabled and is frozen at run acceptance so later configuration edits do not change existing history cards.

## Goals

- Make manual button visibility explicit and configurable per test configuration.
- Keep the OK button and NG-type buttons under one visibility switch; never expose only one side.
- Reorganize the configuration page so the visibility switch and fault-type selection are presented together in an understandable “人工标注” area.
- Preserve automatic analysis, AI labels, manual judgment persistence, reanalysis, and fault-type configuration semantics.
- Preserve old configuration packages and old run-history payloads with a deterministic default of hidden.

## Non-Goals

- Do not add separate OK and NG visibility switches.
- Do not add a global application-wide switch.
- Do not remove fault types from automatic/AI analysis when the manual-button switch is disabled.
- Do not change manual judgment result semantics, labels, colors, or button ordering.
- Do not change reanalysis control visibility.

## Configuration Model

### Persisted field

- Add `manual_judgment_buttons_visible` to `test_config_table`.
- Storage definition: `INTEGER NOT NULL DEFAULT 0` represented as an exact Python `bool` at service and UI boundaries.
- New configurations default to `False`.
- Existing configurations migrated without an explicit value receive `False`.
- `ConfigService.load_config()` always returns the field.
- `ConfigService.save_config()` validates and persists an exact boolean value.
- Configuration copy/import/export includes the field. Older packages without it import as `False`; newly exported packages include it.

### Database migration

- Provide an idempotent migration script that validates the target database, creates a recoverable timestamped backup, and adds the column with `NOT NULL DEFAULT 0` inside a transaction.
- Update runtime schema validation so the application requires the migrated column and validates its exact definition.
- Apply the migration to the workspace database used by this installation after verifying the resolved target path and backup location.

## Configuration UI

- Introduce a clearly labeled “人工标注” configuration area in the global test-configuration section.
- Add one checkbox labeled `在主页结果中显示人工标注按钮`.
- Place the existing fault-type multi-select and `故障类型管理` action in the same area below the checkbox.
- Keep the fault-type selector enabled regardless of the checkbox state because configured fault types remain part of automatic/AI result semantics.
- Loading a configuration populates both the checkbox and fault types.
- Changing either control marks the configuration dirty.
- Saving, discarding, switching configurations, and reloading restore the authoritative persisted values using existing configuration-page behavior.

## Frozen Run Behavior

### History-label payload versioning

- Freeze `manual_judgment_buttons_visible` together with the configuration name and fault types when a run is accepted.
- Introduce a new history-label payload version for newly accepted runs.
- The new version requires an exact boolean field under the frozen configuration object.
- Continue accepting the existing history-label version without the field and interpret it as `False`.
- Preserve canonical hashing/validation separately for each payload version; legacy canonical bytes must remain valid and unchanged.
- Imported runs with an unresolved configuration freeze the value as `False`.

### History projection

- Add the frozen boolean to `FrozenConfigSummary`, defaulting to `False` for legacy/in-process constructors that omit it.
- Preserve the field through result-history cloning, transitions, reanalysis, and manual judgment updates.
- Configuration edits after run acceptance do not change the frozen value on existing cards.

## Homepage Visibility Rules

- The shared manual judgment widget remains hidden unless all current conditions are true:
  - a terminal, non-active result card is selected;
  - manual judgment is operationally allowed for that result;
  - the card’s frozen `manual_judgment_buttons_visible` value is `True`.
- When visible, render the existing green `合格` button plus every frozen configured NG fault-type button.
- If visibility is enabled but the frozen fault-type list is empty, preserve the existing fallback group: `合格` plus generic `不合格`.
- When visibility is disabled, do not attach the shared manual judgment widget to the card and publish no homepage manual-judgment target for that selection.
- Hiding buttons is a presentation decision only; it does not alter stored conclusions, automatic analysis, AI execution, fault-type validation, or backend manual-judgment contracts.

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: [existing configuration values, old configuration packages, legacy history-label payloads and hashes, result-history transitions, manual judgment persistence contracts, reanalysis controls, AI and automatic analysis fault-type semantics]
- Allowed breakage: [manual judgment buttons are hidden by default for existing configurations and legacy history records until a configuration is explicitly enabled for new runs; the configuration page groups fault-type controls under a new visual area]
- Migration strategy: backup plus idempotent database migration script; dual-version history-label decoder; missing package/config values default to false

## Validation and Error Handling

- Reject non-boolean service payload values for `manual_judgment_buttons_visible`.
- Reject malformed new-version history payloads whose field is missing or not an exact boolean.
- Never reinterpret legacy payloads as visible.
- A migration failure must roll back the schema change and leave the original database plus backup available.

## Verification

- Model/schema tests prove the field definition and false default.
- Migration tests prove backup creation, idempotency, preserved rows, false backfill, and rollback on invalid targets.
- Config service and UI tests cover load/save/default/dirty/discard/copy/import/export behavior.
- History payload tests cover new-version round-trip, legacy-version compatibility, canonical hashes, invalid boolean rejection, and run-time freezing.
- Result projection tests prove the frozen flag survives terminal, reanalysis, and manual judgment transitions.
- Homepage tests prove the entire button group is hidden when false and visible when true, including the no-fault-type fallback.
- Regression tests prove the fault-type selector remains usable while the switch is off and AI/automatic result behavior remains unchanged.
