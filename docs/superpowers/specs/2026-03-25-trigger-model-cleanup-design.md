# Trigger Model Cleanup Design

## Context

The current branch contains a `trigger_type` field that was introduced as a placeholder during earlier hardware/config design work and then reused by the factory-mode design as part of PLC eligibility. In practice, that field does not represent the real workflow cleanly:

- Manual start is chosen by factory-mode page state (`manual_primary_config_id`), not by config-owned trigger mode.
- Scan-trigger start is already represented by `scan_triggers_test`.
- PLC-trigger start can be represented directly by saved PLC binding data.

This branch is for a new project. It does not need backward-compatibility behavior or schema-preservation work for `trigger_type`.

## Problem

`trigger_type` collapses multiple independent concerns into one enum-like field:

- runtime trigger source
- config eligibility for scan
- config eligibility for PLC

That creates ambiguous authority and blocks valid combinations. A config may reasonably be:

- manually startable
- scan-enabled
- PLC-bound

at the same time. A single `trigger_type` field is therefore not a scalable model for this product.

## Scope

This spec covers only trigger-model cleanup for the current branch.

In scope:

- remove `trigger_type` from config schema, persistence, UI, tests, and active planning assumptions
- keep `scan_triggers_test` as the only scan-trigger eligibility field
- add or preserve `plc_device_key` and `plc_device_label` as saved PLC metadata
- define PLC eligibility by `plc_device_key` presence only
- define the authoritative trigger rules that future factory-mode implementation must follow

Out of scope:

- PLC hardware protocol work
- PLC discovery/integration UX beyond storing binding metadata
- broader factory-mode implementation work unrelated to this cleanup
- migration logic whose only purpose is preserving `trigger_type`

## Design

### Canonical Trigger Rules

After this cleanup, trigger behavior is defined by explicit fields and page state:

- Manual start: always starts the selected `manual_primary_config_id`
- Scan eligibility: only depends on `scan_triggers_test == True`
- PLC eligibility: only depends on the config having a non-empty `plc_device_key` after trimming surrounding whitespace

`plc_device_label` is display-only metadata and does not affect eligibility.

There is no config-owned field that means "the one true trigger type" for a config.

### Eligibility vs Routing

This cleanup separates config eligibility from runtime routing.

Config fields determine whether a config is eligible for a given non-manual trigger source:

- `scan_triggers_test` determines scan eligibility
- `plc_device_key.strip()` determines PLC eligibility

Runtime routing remains a controller concern. Future factory-mode routing must still evaluate:

- selected pair state
- primary selection
- active/review/reanalysis lock state
- incoming PLC event identity
- tie-break rules between the selected configs

This spec does not move routing authority into config fields. It only removes the redundant `trigger_type` gate from that routing model.

### Source of Truth

The authoritative source of truth for each start path is:

- Manual: factory-mode page/controller selection state
- Scan: `scan_triggers_test`
- PLC: `plc_device_key`

Future routing logic must not branch on `trigger_type`.

### Data Model

Delete `trigger_type` from `TestConfigTable`.

Do not replace it with another enum. The explicit scan field and PLC binding fields already model the actual behavior required by this branch.

This cleanup also owns the minimal PLC metadata path required after `trigger_type` removal:

- `plc_device_key`
- `plc_device_label`

Those fields belong on `TestConfigTable` and are the saved config-level source of PLC binding metadata for future factory-mode routing.

### Config Persistence

`ConfigService` must stop:

- loading `trigger_type`
- saving `trigger_type`
- validating `trigger_type`

This cleanup also requires `ConfigService` to load and save:

- `plc_device_key`
- `plc_device_label`

`ConfigService.validate_factory_runtime()` is limited to hard-blocking per-config runtime validation shared by all start paths, including:

- any per-config validation that does not depend on selected-pair page state or live event payloads

Contract:

- input: one loaded config dict
- output: `list[str]`
- meaning: empty list means there is no hard-blocking config error for starting this config manually; non-empty list contains operator-facing validation errors

Non-eligibility for scan or PLC is not an error. A manual-only config with `scan_triggers_test == False` and blank `plc_device_key` is still valid and should return no errors from `validate_factory_runtime()` unless some separate hard-blocking config problem exists.

Capability checks are separate and explicit:

- scan capability is `scan_triggers_test == True`
- PLC capability is `plc_device_key.strip() != ""`

The future factory-mode controller remains the owner of:

- pair validation
- primary selection requirements
- PLC event matching against the selected pair
- scan/PLC tie-break rules
- blocking starts during active run or reanalysis states

If helper methods are introduced for clarity, they should expose capability questions directly rather than encoding capability as validation errors.

### Config UI

Remove the trigger selector from the config UI.

Keep:

- the existing scan-trigger checkbox because it represents a real independent behavior
- PLC binding inputs because PLC metadata is still part of the branch design

This cleanup owns the minimal PLC metadata editing path needed after removing `trigger_type`:

- plain config inputs for `plc_device_key`
- plain config inputs for `plc_device_label`

No PLC discovery workflow is required here. Simple persisted metadata inputs are enough.

The UI should no longer imply that the operator must choose one trigger mode for a config.

### No Compatibility Layer

Because this is a new project, this cleanup intentionally rejects:

- migration steps whose only purpose is preserving `trigger_type`
- compatibility branches that continue reading `trigger_type`
- fallback routing that consults both `trigger_type` and PLC binding

The branch should move to one clean trigger model instead of carrying dead state forward.

### Database Handling

For this cleanup, "remove `trigger_type` from schema" means:

- remove it from the model definition
- stop adding it in startup migration code
- stop reading and writing it anywhere in application code

This cleanup does not require SQLite drop-column or table-rebuild work just to erase a stale local column from an already-created developer database. If such a column exists in an old local DB file, it is ignored and no longer authoritative.

## Planning Impact

This spec supersedes trigger-related assumptions in these earlier documents:

- `docs/superpowers/specs/2026-03-17-per-config-hardware-trigger-design.md`
- `docs/superpowers/specs/2026-03-20-factory-mode-analysis-ui-design.md`

Specifically, those earlier docs should no longer be treated as authoritative where they:

- define or rely on `trigger_type`
- describe PLC eligibility as depending on `trigger_type == 'plc'`
- describe a trigger-selection UI control as part of the desired design

A new focused implementation plan should be written for this cleanup instead of revising those larger historical documents in place.

The new March 25 cleanup plan becomes the authoritative plan for trigger-model work. Until that plan exists, `docs/superpowers/plans/2026-03-20-factory-mode-analysis-ui.md` must be treated as superseded on trigger-related logic and must not be used as the implementation authority for `trigger_type`.

## Success Criteria

This cleanup is complete when:

- no production code depends on `trigger_type`
- no active plan tells implementers to use `trigger_type`
- config UI no longer presents trigger selection as an enum choice
- PLC eligibility is described and implemented as `plc_device_key` presence only
- scan eligibility is described and implemented as `scan_triggers_test` only

## Verification

- search the repo for remaining production references to `trigger_type`
- confirm the active March 25 plan contains no trigger-type-based routing rules
- confirm `docs/superpowers/plans/2026-03-20-factory-mode-analysis-ui.md` is no longer treated as authoritative for trigger logic once the March 25 cleanup plan is written
- confirm the config UI exposes scan and PLC metadata without a trigger selector
- confirm PLC metadata is persisted on the config record and round-trips through the config UI/service
- run targeted tests for model, config service, config UI, and any factory-routing logic touched by the cleanup
- search docs and tests for stale `trigger_type` references and either remove them or make any historical exceptions explicit
