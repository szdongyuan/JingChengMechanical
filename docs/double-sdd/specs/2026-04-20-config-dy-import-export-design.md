# Config `.dy` Import / Export And AI Package Directory Design

## Summary

Add single-configuration import/export using a custom `.dy` file extension. A `.dy` file is a ZIP-format migration package containing a manifest, one test configuration, and optional assets selected by the user: golden sample WAV files, the bound AI toolchain definition, and the AI package directory.

To make AI packages migratable without guessing from `entrypoint`, extend AI toolchain registration with an explicit required `package_dir` field. `package_dir` is the root directory of the AI tool package. Runtime execution uses that directory as the toolchain working directory, and `.dy` export uses that directory as the authoritative package source when the user chooses to include the AI package.

The app is not yet released, so this design does not preserve legacy AI toolchain rows without `package_dir`. New and edited AI toolchains must provide a valid package directory.

## Goals

- Export the currently selected single configuration to a `.dy` file.
- Import a `.dy` file as a new configuration by default.
- Allow import conflicts to be resolved explicitly through user confirmation.
- Support optional asset inclusion during export:
  - golden sample WAV files
  - bound AI toolchain metadata
  - bound AI package directory
- Make AI toolchains self-describing enough for migration by adding a required `package_dir`.
- Keep input/output hardware binding display behavior unchanged. Imported configs may still reference unavailable input/output devices, and the existing UI should continue showing unavailable devices as it does today. Imported PLC bindings are cleared because PLC rows are local database identities.
- Keep the implementation localized to config, AI toolchain, golden-sample, packaging, and tests. Do not modify hardware management code.

## Non-Goals

- Batch import/export of all configurations.
- Import/export from the hardware management module.
- Rewriting hardware identities, auto-mapping devices, or validating target-machine hardware during import.
- Inferring AI package contents from `entrypoint`.
- Supporting AI tools that depend on files outside `package_dir`.
- Changing the AI request/response protocol.
- Migrating historical AI execution records.
- Supporting legacy released `.dy` package formats.

## Existing Context

Current config state is split across:

- `test_config_table` for top-level test config data.
- `channel_config_table` for per-channel analysis, limit, golden-sample, and chart settings.
- `test_config_ai_binding_table` for one optional test-level AI toolchain binding.
- `ai_toolchain_table` for global AI toolchain metadata.
- Managed golden sample WAV files under `GoldenSampleService.managed_root()`.

Current AI toolchain metadata knows how to launch a toolchain, but does not know the full package boundary. The existing fields include `toolchain_id`, `display_name`, `version`, `entrypoint`, `protocol_version`, `execution_mode`, `default_timeout_ms`, `enabled`, and `description`. This is insufficient for reliable migration because `entrypoint` may reference only one script while the tool depends on adjacent model/config files.

## Product Decisions

### Package Format

- File extension: `.dy`.
- Container format: ZIP.
- The extension is intentionally not `.zip` so operators treat it as an application-specific migration package.
- The package must contain a root manifest named `manifest.json`.
- The implementation must validate ZIP entry names to prevent path traversal on import.

### Export Scope

- Export only the currently selected single configuration.
- Export is unavailable when no configuration is selected.
- Export must write a package that can be imported on another machine.
- Export options:
  - `包含黄金样本`
  - `包含 AI 工具链`
  - `包含 AI 包`
- `包含 AI 包` requires `包含 AI 工具链`.
- `包含 AI 包` requires that the bound AI toolchain has a valid `package_dir`.
- `包含 AI 工具链` without `包含 AI 包` is allowed only as metadata/reference export. On another machine it can bind to an existing local toolchain with the same `toolchain_id`, but it cannot create or overwrite a runnable toolchain because `package_dir` is mandatory and package files are absent.
- If the config has no AI binding or AI is not bound to a toolchain, AI options are disabled or produce a clear validation message.

### Import Scope

- Import one `.dy` package at a time.
- Import creates a new configuration by default.
- If the package contains golden samples, import restores them into the local managed golden-sample directory and rewrites channel bindings to the restored local paths.
- If the package contains an AI package, import restores it into a local managed AI package directory and rewrites the imported toolchain `package_dir` to that local path.
- If the package contains AI metadata but not an AI package:
  - if a local toolchain with the same `toolchain_id` exists and the user chooses `使用旧工具链`, import binds the config to that local toolchain.
  - if no local toolchain exists, or the user does not choose an existing local toolchain, import disables/clears the imported AI binding and shows a warning.
  - import must not create or overwrite an AI toolchain row from metadata-only package content.
- Hardware device fields are imported as config data. Input/output device name, host API, and sample-rate fields are preserved exactly so existing unavailable-device display behavior can continue.
- PLC binding is always cleared on import by setting `plc_id` to `None`. `plc_id` is a local auto-increment database foreign key, so reusing a source-machine numeric ID could bind the imported config to an unrelated target-machine PLC. This does not modify hardware management code and does not create placeholder PLC rows.

### Conflict Resolution

Import must detect conflicts before mutating local state and show a confirmation dialog.

Configuration name conflict:

- `覆盖`: replace the existing local config with the imported config data and selected assets.
- `使用旧配置`: keep the existing local config and skip the entire package import. No AI toolchain, AI package, or golden-sample assets from this `.dy` file are processed.
- `取消`: abort the full import.

Configuration conflicts are detected by `config_name`. `test_config_table.config_name` remains unique, so duplicate names are not allowed. Source-machine `config_id` is informational only and is never used to match an existing local row.

AI toolchain behavior depends on whether the `.dy` package includes AI package files.

When the package includes both AI toolchain metadata and AI package files:

- If no local `toolchain_id` exists, import creates the local AI toolchain and restores the AI package.
- If a local `toolchain_id` exists, show conflict actions:
  - `覆盖`: replace the existing local AI toolchain metadata, and replace the local AI package if the package includes one.
  - `使用旧工具链`: keep the existing local AI toolchain and bind the imported config to it.
  - `取消`: abort the full import.

When the package includes AI toolchain metadata but no AI package files:

- Import must not show a toolchain `覆盖` action.
- If a local toolchain with the same `toolchain_id` exists, import shows actions:
  - `使用旧工具链`: bind the imported config to the existing local toolchain. This is the default action.
  - `清空 AI 绑定`: import the config but disable and clear its AI binding.
  - `取消`: abort the full import.
- If no local toolchain with the same `toolchain_id` exists, import clears the imported config's AI binding and shows a warning that the package did not include a runnable AI package.
- Import must not create or overwrite an AI toolchain row from metadata-only content.

If the package contains no AI toolchain metadata:

- Import clears the imported config's AI binding and shows no AI toolchain conflict action.

Conflict action labels for runnable AI packages:

- `覆盖`: replace the existing local AI toolchain metadata, and replace the local AI package if the package includes one.
- `使用旧工具链`: keep the existing local AI toolchain and bind the imported config to it.
- `取消`: abort the full import.

`覆盖` is not offered for metadata-only packages. `使用旧工具链` is offered for metadata-only packages only when a matching local toolchain exists.

The dialog must make clear that overwriting an AI toolchain can affect other configurations bound to the same `toolchain_id`.

### AI Package Directory Convention

- Add `package_dir` to `ai_toolchain_table`.
- `package_dir` is required for new and edited AI toolchains.
- `package_dir` must be a valid existing directory when saving a toolchain.
- `entrypoint` is interpreted relative to `package_dir` for relative script paths and assets.
- Runtime execution must use `package_dir` as the subprocess working directory.
- AI tools that reference files outside `package_dir` are considered non-portable tool packages. If those external references break after migration, that is a tool packaging error, not a `.dy` export/import bug.
- Persisted `package_dir` values are always local filesystem paths. Package-internal ZIP paths are stored separately and must not be persisted directly as `package_dir`.

## `.dy` Package Layout

Recommended ZIP layout:

```text
manifest.json
config/config.json
golden_samples/channel_<channel_index>.wav
ai/toolchain.json
ai/package/<toolchain package files...>
```

### Manifest

`manifest.json` contains:

```json
{
  "format": "jingcheng-config-package",
  "format_version": 1,
  "app": "JingChengMechanical",
  "exported_at": "ISO-8601 timestamp",
  "contents": {
    "config": true,
    "golden_samples": true,
    "ai_toolchain": true,
    "ai_package": true
  }
}
```

### Config Payload

`config/config.json` contains one config payload shaped like `ConfigService.load_config(config_id)`, plus optional import metadata:

```json
{
  "config": {
    "config_name": "Example",
    "record_duration": 5.0,
    "analysis_duration": 3.0,
    "...": "..."
  },
  "ai_binding": {
    "enabled": true,
    "toolchain_id": "motor-ai",
    "timeout_ms": null,
    "params_json": "{}"
  },
  "golden_sample_assets": {
    "0": "golden_samples/channel_0.wav"
  }
}
```

Implementation notes:

- `config_id` from the source machine is always informational and must never be reused as a local identity. New imports allocate a fresh local `config_id`. Overwrite imports update the local row matched by `config_name` and keep that local row's existing `config_id`.
- Imported `created_time` / `updated_time` from the source package are not authoritative. Local rows use local timestamps.
- Golden sample absolute source paths are not trusted across machines. If an asset is included, import rewrites the channel's `golden_sample_path` to the restored local managed path. If an asset is not included, imported golden bindings are cleared and golden modes/overlays are set to `off` / `False` so the imported config remains valid.

### AI Toolchain Payload

`ai/toolchain.json` contains the source toolchain metadata:

```json
{
  "toolchain_id": "motor-ai",
  "display_name": "Motor AI",
  "version": "1.0",
  "entrypoint": "python main.py",
  "protocol_version": 1,
  "execution_mode": "resident_process",
  "default_timeout_ms": 30000,
  "enabled": true,
  "description": "...",
  "source_package_dir": "D:/source-machine/ai/motor-ai",
  "package_archive_path": "ai/package"
}
```

On import:

- If the AI package is included, restore it to a local managed AI package directory and rewrite `package_dir`.
- If the AI package is not included, use conflict resolution:
  - If using an existing local toolchain, keep its local `package_dir`.
  - If creating or overwriting from metadata without package files, the operation is not allowed because there is no local package directory to persist.

Field meanings:

- `source_package_dir` is for operator diagnostics only. It is the source-machine directory at export time and is never trusted as a target-machine path.
- `package_archive_path` is a package-relative directory inside the `.dy` ZIP. It is present only when AI package files are included.
- Persisted imported toolchain rows use a locally restored absolute `package_dir`.

## Managed AI Package Storage

Add an application-owned root for imported AI packages, for example:

```text
data/ai_toolchains/<sanitized_toolchain_id>_<sha256_16>/
```

Rules:

- Importing an AI package creates or replaces a directory under the managed root.
- The target directory name must be sanitized so `toolchain_id` cannot escape the managed root.
- To avoid collisions between different IDs that sanitize to the same text, the managed directory name must include both a sanitized prefix and a stable hash of the original `toolchain_id`, for example:

```text
data/ai_toolchains/<sanitized_toolchain_id>_<sha256_16>/
```

- Each managed package directory must include a marker file recording the original `toolchain_id`.
- If the computed directory already exists with the same marker, it is the managed directory for that same toolchain and may be overwritten through the normal overwrite flow.
- If the computed directory exists with a different marker, append a deterministic numeric suffix such as `_2`, `_3`, etc. until an unused directory or matching-marker directory is found.
- If overwriting an existing managed AI package, first move the old managed package directory to a temporary backup path, then copy/extract the new package to the final path. If the import fails before commit, remove the new directory and restore the backup. If import succeeds, delete the backup.
- If the existing local `package_dir` is outside the managed root and the user chooses `覆盖`, the imported toolchain row points to the new managed package path, but the old external directory is not deleted.

## Service Design

### Config Package Service

Create a focused service, tentatively `ConfigPackageService`, responsible for `.dy` package import/export.

Responsibilities:

- Build export payloads from:
  - `ConfigService.load_config(config_id)`
  - `AiService.resolve_binding(config_id)`
  - `AiService.get_toolchain(toolchain_id)`
  - `GoldenSampleService` managed paths
- Write `.dy` ZIP files with safe relative names.
- Inspect `.dy` packages before import to produce a preview and conflict report.
- Import packages through explicit, testable operations:
  - create new config
  - overwrite existing config
  - create/overwrite/use existing AI toolchain
  - restore golden samples
  - restore AI package
- Roll back imported assets if database writes fail.

Import ordering:

1. Read and validate the `.dy` file into a temporary extraction directory.
2. Build an import plan from user conflict decisions before mutating local data.
3. Determine all final asset paths before database writes.
4. For overwrites, back up existing managed assets that may be replaced.
5. Copy/extract required new golden samples and AI package files to their final local managed paths before committing database rows that point at them.
6. Perform database writes in one transaction where possible.
7. If database writes fail before commit, remove newly copied assets and restore backups.
8. If database writes commit successfully, the imported configuration is valid because all required final assets already exist. Backup cleanup after commit is best-effort and may log warnings, but it is not part of required import finalization.

### ConfigService

Add service boundaries needed by import/export without teaching UI code about table internals:

- Export-safe config serialization for one `config_id`.
- Create config from imported payload with a fresh local `config_id`.
- Overwrite an existing config from imported payload.
- Preserve existing save validation semantics.
- Provide import-safe validation/persistence that preserves input/output hardware text fields but always normalizes imported `plc_id` to `None` before calling the normal save boundary.
- Reuse golden-sample promotion/cleanup patterns where possible.

### AiService

Extend AI toolchain persistence and runtime behavior:

- `AiToolchainTable.package_dir`.
- `save_toolchain()` validates `package_dir`.
- `list_toolchains()` / `list_enabled_toolchains()` include `package_dir`.
- `resolve_binding()` remains binding-focused and does not need to include package internals.
- Runtime preflight requires bound toolchain `package_dir` to exist.
- Runtime subprocess execution uses `package_dir` as `cwd`.
- Import/export helpers may reuse `save_toolchain()` for metadata writes.

### GoldenSampleService

Add small asset helpers if needed:

- Copy a package-contained golden sample into the managed committed directory for a target config/channel.
- Validate restored files with existing committed-binding validation rules.
- Roll back restored imported files on failure.

When a package does not include selected golden sample assets, import must clear the exact channel fields below:

```text
golden_sample_path
golden_sample_label
golden_sample_source_channel
golden_sample_start_sample
golden_sample_num_samples
golden_sample_start_ms
golden_sample_duration_ms
golden_sample_sample_rate
```

and also set:

```text
fft_golden_mode = "off"
fft_show_golden_overlay = False
oct_golden_mode = "off"
oct_show_golden_overlay = False
```

This keeps imported configs valid when the `.dy` package intentionally omits WAV assets.

## UI Design

### Config Page

Add buttons near the existing config list actions:

- `导入`
- `导出`

Behavior:

- `导出` requires a selected config.
- `导入` opens a `.dy` file chooser.
- File dialogs filter `*.dy`.
- Export opens an options dialog before choosing or writing the file.
- Import opens a preview/conflict confirmation dialog before applying changes.
- After successful import, refresh the config list and select the imported or overwritten config.

### Export Options Dialog

Fields:

- `包含黄金样本`
- `包含 AI 工具链`
- `包含 AI 包`

Validation:

- `包含 AI 包` is disabled unless `包含 AI 工具链` is selected.
- If `包含 AI 包` is selected and no valid bound toolchain/package directory exists, show a validation error and do not export.
- If `包含黄金样本` is selected but one or more bound golden sample files are missing, show a validation error and do not export.

### Import Confirmation Dialog

The dialog shows:

- Source config name.
- Whether golden samples are included.
- Whether AI toolchain metadata is included.
- Whether AI package files are included.
- Conflict rows and chosen action.

At minimum, support action choices:

- `覆盖`
- `使用旧配置` / `使用旧工具链`
- `清空 AI 绑定` for metadata-only AI imports when a matching local toolchain exists but the user does not want to bind to it
- `取消`

For first implementation, it is acceptable to use one confirmation dialog per conflict type if that is simpler and clearer.

If the user chooses `使用旧配置` for a config-name conflict, the import stops successfully with no mutation and an informational message that the existing local config was kept.

## Validation And Security

Export:

- Before exporting an AI package, resolve `package_dir` to its real path and require it to be a directory.
- Walk the package directory without following symlinks, junctions, or other reparse points.
- Reject symlinks, junctions, reparse points, or any file whose real path is not inside the resolved `package_dir`.
- Reject duplicate archive names produced by normalization, including case-insensitive collisions on Windows.
- Apply the same file-count, uncompressed-size, and normalized path-length limits used by import validation before writing the package.

Import:

- Reject non-ZIP or unreadable `.dy` files.
- Reject missing `manifest.json`.
- Reject unsupported `format` or `format_version`.
- Reject ZIP entries with absolute paths, drive prefixes, or `..` traversal.
- Reject duplicate normalized entry names.
- On Windows, reject case-insensitive normalized path collisions, for example `AI/Package/model.bin` and `ai/package/model.bin` in the same archive.
- Reject packages where `contents.config` is not exactly `true`.
- Reject packages missing `config/config.json`.
- Reject packages with additional config payload files under `config/` other than `config/config.json`; `.dy` version 1 is a single-config package format.
- Reject packages whose manifest claims assets that are missing.
- Reject package extraction that would leave the managed root.
- Reject contradictory manifest/package combinations:
  - `contents.ai_package` is `true` but `contents.ai_toolchain` is not `true`.
  - `contents.ai_package` is `true` but `ai/toolchain.json` is missing.
  - `contents.ai_package` is `true` but `ai/toolchain.json` has no `package_archive_path`.
  - `contents.ai_package` is `true` but there is no file entry under the exact `package_archive_path/` directory-boundary prefix. Directory entries alone do not count because ZIP archives may omit them. A value such as `ai/package` matches `ai/package/model.bin` but does not match `ai/package_evil/model.bin`.
  - `contents.ai_toolchain` is `false` but `ai/toolchain.json` is present.
- Enforce extraction limits before writing files:
  - maximum uncompressed package payload: 4 GiB
  - maximum file count: 50,000 entries
  - maximum single entry path length after normalization: 240 characters on Windows
  - reject symlink-like entries or special file metadata if exposed by the ZIP entry attributes
- Do not execute package contents during import.
- Validate imported config data with existing `ConfigService.validate()`.
- Validate imported AI toolchain data with existing/new `AiService.save_toolchain()` validation.
- Validate restored golden sample files before committing the config.

## Failure Handling

Export:

- On validation failure, no partial output file should be left behind if practical.
- Missing optional assets selected by the user are hard errors.

Import:

- Import is all-or-nothing for the selected operation.
- If database save fails, restore or remove any imported golden samples and AI package files created during the attempt.
- Overwriting an existing config must not delete its old managed golden sample files until the replacement config save has committed successfully.
- Overwriting an existing local managed AI package must preserve a backup until toolchain and config database writes have committed successfully.
- When using an existing local AI toolchain, import must not mutate that toolchain row or package directory.
- If AI package restore fails, do not create/overwrite the config.
- If golden sample restore fails, do not create/overwrite the config.
- User cancellation causes no mutation.

## Compatibility / Migration

- Backward compatibility: not required.
- Protected surfaces:
  - hardware management module code
  - current hardware unavailable display behavior
  - AI request/response protocol
  - single-config save/load behavior outside import/export
  - golden sample validation semantics
- Allowed breakage:
  - `ai_toolchain_table` schema adds required `package_dir`
  - AI toolchain create/edit validation now requires a valid package directory
  - runtime fails earlier if a bound toolchain has an invalid package directory
  - tests and fixtures that create toolchain rows must provide `package_dir`
- Migration strategy: none for released users. Startup DB repair may add the column for local development databases, but rows without a valid package directory are considered invalid until corrected.

## Testing Strategy

### Unit / Service Tests

- AI toolchain save rejects missing or non-directory `package_dir`.
- AI toolchain save/list/load round-trips `package_dir`.
- AI runtime invokes subprocess/resident startup with `cwd=package_dir`.
- `.dy` export writes expected manifest and config payload.
- `.dy` export includes golden sample files when selected.
- `.dy` export includes toolchain metadata and package files when selected.
- `.dy` export rejects `include_ai_package=True` without a valid bound package directory.
- `.dy` import creates a new config by default.
- `.dy` import restores golden sample WAV files to the local managed directory.
- `.dy` import restores AI package files to the local managed directory.
- `.dy` import overwrites existing config only after confirmed conflict action.
- `.dy` import uses existing local AI toolchain when requested.
- `.dy` import overwrites local AI toolchain and package when requested.
- `.dy` import rejects unsafe ZIP paths.
- `.dy` import rolls back restored assets when database writes fail.

### UI Tests

- Config page renders `导入` / `导出`.
- Export action is disabled or warns when no config is selected.
- Export options enforce `AI 包` depends on `AI 工具链`.
- Import success refreshes and selects the imported config.
- Import conflict dialogs route `覆盖`, `使用旧配置` / `使用旧工具链`, and `取消`.
- Metadata-only AI import conflict dialog routes `使用旧工具链`, `清空 AI 绑定`, and `取消`.
- AI tool management page shows and saves `AI 包目录`.

### Regression Commands

Focused tests:

```powershell
pytest tests/test_config_service.py tests/test_config_module.py tests/test_ai_service.py tests/test_ai_tool_management_module.py tests/test_golden_sample_service.py -q
```

Full suite:

```powershell
pytest tests -q
```

## Implementation Notes

- Prefer a new packaging service instead of putting ZIP read/write logic directly in `ConfigModule`.
- Keep UI code thin: collect user choices, call services, show results.
- Keep hardware modules untouched.
- Use `zipfile` from the standard library.
- Use temporary directories/files during import/export to support rollback.
- Normalize all paths with `os.path.abspath` / `os.path.realpath` before safety checks.
- Sanitize `toolchain_id` before using it as a directory name.
