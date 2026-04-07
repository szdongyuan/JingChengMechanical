# AI Tool Management Page Design

## Summary

The application already persists global AI toolchains and lets test configs bind to enabled toolchains, but the current UI has no direct entry point to register or manage those toolchains. This pass adds a dedicated top-level `AI工具管理` page for global AI toolchain registry CRUD while keeping the per-config AI binding card inside the existing config page.

The user-approved product rules for this pass are:

- add a new top-level `AI工具管理` tab
- show that tab to `管理员` and `工程师`
- keep per-config AI binding in the existing `配置` page
- allow create, edit, enable/disable, and permanent delete of registered toolchains
- allow editing `toolchain_id`, `display_name`, `version`, `entrypoint`, `default_timeout_ms`, `enabled`, and `description`
- when `toolchain_id` changes, update all binding rows that still reference the old id to the new id
- do not rewrite historical `ai_execution_table` rows during rename
- allow permanent delete even if configs currently reference the toolchain
- keep `protocol_version` and `execution_mode` fixed to the currently supported runtime values
- do not add backward-compatibility-specific work for this pass

## Problem

### Missing Registration Entry Point

Current UI only exposes a toolchain selection combo in `ConfigModule`. It assumes toolchain rows already exist in `ai_toolchain_table`. There is no direct in-app entry point to create, edit, or delete those registry rows.

### Split Responsibility Without a Registry Surface

The current config page mixes test-config editing with AI binding selection, but the global registry that backs that selector is only manageable through direct DB edits or external scripts. This makes routine admin tasks fragile:

- registering a new toolchain
- updating an entrypoint or timeout
- disabling a bad toolchain without deleting it
- removing retired toolchains

### Unsafe Admin Workflows

Because registry management is not surfaced in the UI, the only current way to manipulate toolchains is direct database mutation. That bypasses user confirmation, hides binding impact, and makes rename/delete behavior easy to get wrong.

## Scope

In scope:

- add a new top-level `AI工具管理` page
- expose global AI toolchain registry CRUD in the app
- keep per-config AI binding in the existing config page
- allow editing `toolchain_id`, `display_name`, `version`, `entrypoint`, `default_timeout_ms`, `enabled`, and `description`
- keep `protocol_version` and `execution_mode` fixed to current supported values
- support permanent delete with impacted-config confirmation
- support `toolchain_id` rename with binding rewrite
- refresh dependent UI when registry rows change
- regression coverage for new navigation, permissions, service behavior, and UI flows

Out of scope:

- moving AI binding out of `ConfigModule`
- changing the AI request/response protocol
- supporting additional `protocol_version` values
- supporting additional `execution_mode` values
- automatic toolchain discovery from files, folders, or manifests
- running a toolchain self-test or dry-run from the page
- bulk edit, search, pagination, import/export, or audit screens
- rewriting historical `ai_execution_table` rows during rename or delete

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - current per-config AI binding workflow in `ConfigModule`
  - current AI runtime execution contract in `AiService`
  - current `ai_toolchain_table`, `test_config_ai_binding_table`, and `ai_execution_table` schema shapes
- Allowed breakage:
  - main-window top-tab count and index ordering
  - role-to-tab availability now that `AI工具管理` is added
  - previous implicit assumption that `ai_toolchain_table` is managed only outside the app
- Migration strategy: none; reuse the current schema and add no compatibility layer or legacy UI preservation behavior in this pass

## Approved Decisions

1. Add a new top-level tab named `AI工具管理`.
2. Insert it into the main navigation after `配置`.
3. Show it to `管理员` and `工程师`; `操作员` does not get access.
4. Keep the existing AI binding card in the `配置` page; this new page is only for the global registry.
5. The page must support create, edit, save, enable/disable, and permanent delete.
6. Editable registry fields are:
   - `toolchain_id`
   - `display_name`
   - `version`
   - `entrypoint`
   - `default_timeout_ms`
   - `enabled`
   - `description`
7. `protocol_version` is fixed to `1` and `execution_mode` is fixed to `oneshot_process` in this pass; they are not user-editable.
8. Permanent delete is allowed even if configs currently reference the toolchain.
9. Delete confirmation must warn about affected configs and deletion must clear those bindings before removing the toolchain row.
10. Editing `toolchain_id` is treated as a rename of the active registry entry and must update all binding rows that still reference the old id to the new id.
11. Renaming does not rewrite historical `ai_execution_table` rows.
12. No backward-compatibility-specific implementation work is required for this pass.

## Design Summary

Introduce a dedicated registry-management module that follows the established `ConfigModule` master/detail pattern:

- left list for global AI tools
- right detail form for the selected tool
- empty state when no tools exist
- `新建`, `删除`, and `保存` actions
- confirmation dialog for destructive delete
- one transactional service boundary for create/update/rename/delete

This keeps test-config binding and global toolchain registry concerns separate while making toolchain registration a first-class workflow in the main application.

Dirty edits in this module are never discarded silently. Selection changes, `新建`, tab switches, and window close all use the same unsaved-changes decision contract described below.

## Main Window Integration

### Navigation

`MainWindow` adds a new page builder and top tab:

- tab label: `AI工具管理`
- placement: after `配置`
- resulting default tab order:
  - `测试`
  - `数据管理`
  - `配置`
  - `AI工具管理`
  - `硬件`

The page is a standard card-wrapped module like the existing `数据管理` and `配置` pages.

### Role Permissions

Update `ROLE_PERMISSIONS` so:

- `管理员` can access `AI工具管理`
- `工程师` can access `AI工具管理`
- `操作员` cannot access `AI工具管理`

No new user-menu actions are introduced in this pass.

## Page Layout

### Overall Structure

The new module uses a two-pane master/detail layout consistent with `ui/modules/config.py`:

- left pane: toolchain list and primary actions
- right pane: editable detail form for the selected toolchain
- empty state text when the registry is empty

### Left Pane

The list pane must:

- show all registered toolchains, not only enabled ones
- sort by `toolchain_id`
- display enough context to distinguish rows, at minimum `display_name` and `version`
- visibly differentiate disabled rows, for example via a suffix or muted styling
- support single selection only

Action buttons:

- `新建`
- `删除`

`删除` is disabled when nothing is selected.

### Right Pane

The detail pane edits one selected toolchain and includes:

- `toolchain_id` text input
- `display_name` text input
- `version` text input
- `entrypoint` text input or multi-line plain-text field sized for longer commands
- `default_timeout_ms` integer editor in milliseconds
- `enabled` checkbox
- `description` multi-line plain-text field
- read-only helper text noting:
  - `protocol_version = 1`
  - `execution_mode = oneshot_process`

The detail pane includes a single primary `保存` action.

### Dirty-State Rules

The module must track whether the current editor contents differ from the last loaded or last successfully saved state.

When the editor is dirty and the user tries to:

- select a different toolchain row
- click `新建`
- click `删除`
- switch away from the `AI工具管理` tab
- close the main window

the module must prompt with a three-way unsaved-changes decision:

- save and continue
- discard changes and continue
- cancel and stay on the current editor state

Required behavior:

- cancel keeps the current selection and editor unchanged
- discard reloads the target row or empty draft state without saving
- save attempts the normal validation and persistence path first
- if save fails, navigation does not continue and the current editor stays visible

If the original user action was `删除`:

- run the unsaved-changes decision first
- if the user chooses save and save succeeds, continue into the normal delete confirmation flow for the now-saved row
- if the user chooses discard, restore the persisted row and then continue into the normal delete confirmation flow
- if the user cancels or save fails, do not open the delete confirmation dialog

This pass does not introduce auto-save.

### Empty State

When no toolchains exist:

- the detail editor is hidden
- an empty-state label explains that no AI tools are registered and the user should click `新建`

After the first successful create:

- the new row becomes selected
- the detail pane becomes visible

## Registry CRUD Contract

### Service Ownership

Extend `AiService` to own global registry CRUD so registry logic stays near the existing binding/runtime logic.

Required registry-oriented boundaries:

- list all toolchains for management, including disabled rows
- create a new toolchain row
- update an existing toolchain row
- delete a toolchain row
- inspect configs currently bound to a toolchain for confirmation messaging, returning both `config_id` and `config_name`

The config page continues to use the existing binding-resolution APIs.

### Create

Create must:

- validate input fields before writing
- persist fixed internal values:
  - `protocol_version = 1`
  - `execution_mode = oneshot_process`
- reject duplicate `toolchain_id`
- persist trimmed user-entered strings

Required validation:

- `toolchain_id` must be a non-empty unique string
- `display_name` must be non-empty
- `entrypoint` must be non-empty
- `default_timeout_ms` must be a positive integer
- `version` and `description` may be empty strings

This pass does not validate that the entrypoint executable actually exists or runs successfully.

### Update Without ID Change

Normal update of an existing toolchain must:

- keep the same primary key
- update editable user fields
- overwrite `updated_time`
- preserve fixed internal values (`protocol_version`, `execution_mode`)

### Update With `toolchain_id` Rename

If the selected toolchain is saved with a different `toolchain_id`, the operation is a rename and must be transactional.

Required behavior:

- validate the new `toolchain_id` as non-empty and unused
- preserve the edited toolchain's current user-visible fields
- preserve the logical toolchain row's `created_time`
- overwrite the logical toolchain row's `updated_time`
- rewrite all `test_config_ai_binding_table.toolchain_id` references from the old id to the new id, regardless of whether the binding row is currently enabled or disabled
- preserve each affected binding row's identity and data other than the foreign key, including `binding_id`, `config_id`, `enabled`, `timeout_ms`, `params_json`, and `score_threshold`
- overwrite each affected binding row's `updated_time`
- leave `ai_execution_table` rows untouched
- emit the same registry-changed notification as any other save

Implementation note:

- the code may realize rename either by a safe primary-key update sequence or by transactional create-rebind-delete behavior, as long as the observable contract above holds

### Delete

Delete is permanent and allowed even when configs currently reference the toolchain.

Required behavior:

- before deletion, resolve the set of affected configs
- show a confirmation dialog that includes:
  - the selected toolchain identity
  - the affected-config count
  - the affected config names, with ids available to the dialog text if needed
  - that those configs will become unbound if deletion proceeds
- if the user confirms, clear affected bindings before deleting the toolchain row
- explicit unbind behavior means:
  - `enabled = False`
  - `toolchain_id = NULL`
  - preserve existing `timeout_ms`, `params_json`, and `score_threshold`
  - preserve `binding_id` and `config_id`
  - overwrite `updated_time`
- after delete, the removed toolchain no longer appears in management lists or enabled-selector lists

Delete must not rewrite or delete historical `ai_execution_table` rows.

### Enable / Disable

The `enabled` field remains part of the global registry row.

Required behavior:

- disabled toolchains remain visible and editable in `AI工具管理`
- disabled toolchains are excluded from the normal enabled-only selector population in `ConfigModule`
- if a config is already bound to a now-disabled toolchain, `ConfigModule` continues its current behavior of injecting the unavailable selection so the user can see what is bound

## UI Refresh / Notification Contract

Registry changes affect other living UI modules, especially the config page's toolchain selector. Add a dedicated bus notification for registry changes.

Required behavior:

- add an event-bus signal named `ai_toolchains_changed`
- emit it after successful create, update, rename, delete, or enable/disable save
- `ConfigModule` listens for this signal
- if the current config UI has no unsaved local AI-binding edits, `ConfigModule` may re-resolve the binding and refresh the AI card immediately
- if the current config UI has unsaved local AI-binding edits, `ConfigModule` must not silently overwrite those local values; it may refresh option availability only, but any full binding reload must be deferred until the next explicit config reload or successful save cycle
- when `ConfigModule` later saves a local AI binding, it must validate the selected `toolchain_id` against the current registry state before persisting
- if the locally selected `toolchain_id` no longer exists or is no longer enabled, save must fail with a user-visible validation message that requires the operator to reselect a valid toolchain or clear the binding
- `ConfigModule` must not silently auto-remap a preserved unsaved local selection to a renamed toolchain id during save

Observable consequences:

- renamed toolchains appear under the new id in the config page without requiring app restart
- deleted toolchains disappear from selector options and affected configs refresh to an unconfigured state when no unsaved local AI-binding edits need to be preserved
- newly created or re-enabled toolchains appear in enabled-selector options immediately
- preserved unsaved local AI edits remain editable, but stale toolchain selections are rejected explicitly at save time instead of being rewritten implicitly

## Validation and Error Handling

### Save Validation

Save attempts must surface inline or dialog validation failures for:

- missing required text fields
- duplicate `toolchain_id`
- invalid `default_timeout_ms`

Validation failures do not partially write registry or binding data.

### Transaction Boundaries

Operations that touch both registry rows and binding rows must be atomic:

- `toolchain_id` rename
- delete with affected-binding clearing

If the operation fails mid-way:

- no partial rewrite of bindings is allowed
- the selected toolchain remains unchanged

### Runtime Fields

This page does not let the user modify runtime-unsupported registry fields. The service must not accept caller-provided alternate values for:

- `protocol_version`
- `execution_mode`

Even if future internal callers pass those keys, this pass normalizes them to the current supported values.

## Testing Strategy

Add or update regression coverage for:

- main-window top-tab creation and ordering with the new `AI工具管理` page
- role-based visibility/access for `管理员`, `工程师`, and `操作员`
- AI tool management module empty state, selection flow, create flow, edit flow, delete flow, and save validation
- `AiService` registry CRUD behavior
- `toolchain_id` rename updating active config bindings while preserving binding payload fields
- delete clearing affected bindings before removing the toolchain row
- delete leaving `ai_execution_table` rows untouched
- fixed `protocol_version` and `execution_mode` behavior on create/update
- config-page combo refresh after `ai_toolchains_changed` emissions
- disabled toolchains remaining manageable but excluded from normal enabled-only selection lists

Verification should include:

- targeted `AiService` tests
- targeted new module UI tests
- updated `MainWindow` permission/navigation tests
- targeted `ConfigModule` refresh tests
- full `pytest` suite before implementation completion

## Files Likely Affected

- `app/main_window.py`
- `app/services_container.py`
- `app/service/ai_service.py`
- `ui/modules/config.py`
- `ui/modules/ai_tool_management.py`
- `tests/test_main_window.py`
- `tests/test_ai_service.py`
- `tests/test_config_module.py`
- new AI tool management UI tests under `tests/`

## Non-Goals

This design does not introduce:

- protocol negotiation or protocol-version editing
- multiple execution-mode support
- toolchain process self-test or health-check actions
- automatic registry import from the filesystem
- bulk management workflows
- historical execution rewrite on rename or delete
