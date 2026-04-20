# Config `.dy` Import / Export Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build single-configuration `.dy` import/export with optional golden samples, AI toolchain metadata, and AI package directory migration.

**Architecture:** Keep ZIP/package logic in a new focused `ConfigPackageService`; keep table-level persistence in existing config/AI services; keep UI code thin and limited to collecting choices, showing conflict confirmations, and calling service APIs. Do not modify hardware management modules; imported input/output hardware fields should continue flowing through the existing config display behavior, and imported PLC bindings must be normalized to `None`.

**Tech Stack:** Python, Peewee, PyQt6, standard-library `zipfile`, `tempfile`, `shutil`, pytest.

---

## Guardrails

- Do not modify hardware management code or device enumeration behavior.
- Do not infer AI package contents from `entrypoint`; use only `package_dir`.
- Do not touch unrelated modules except for tests/fixtures that need `package_dir`.
- Preserve current config save/load behavior outside import/export.
- Use TDD for each task. Add or update tests before implementation.
- After each task, run the specified focused tests, commit only files from that task, and send the diff through paired `spec-code-reviewer` and `quality-code-reviewer`.

## File Map

- Create `app/service/config_package_service.py`: `.dy` export, ZIP validation, import preview/conflict planning, import execution, managed AI package restore, rollback orchestration.
- Modify `app/services_container.py`: instantiate `ConfigPackageService` as `config_package_service`.
- Modify `model/ai_toolchain_table.py`: add required `package_dir`.
- Modify `app/service/db_connection_service.py`: add startup column repair for `ai_toolchain_table.package_dir`.
- Modify `app/service/ai_service.py`: validate and persist `package_dir`, expose it in lists, preflight it, and pass it to process execution.
- Modify `app/service/ai_resident_runtime.py`: include `package_dir` in resident process cache identity and use it as subprocess `cwd`.
- Modify `ui/modules/ai_tool_management.py`: show, browse, validate, save, and dirty-track `AI 包目录`.
- Modify `app/service/config_service.py`: add import-safe create/overwrite boundaries and golden-binding clearing helper.
- Modify `app/service/golden_sample_service.py`: add helper to commit a package-restored WAV file into the managed config/channel directory.
- Create `ui/popups/config_package_dialogs.py`: export options dialog and import preview/conflict decision helpers.
- Modify `ui/modules/config.py`: add `导入` / `导出` buttons and call the new popup helpers; do not change hardware selector internals.
- Add `tests/test_config_package_service.py`: service-level `.dy` export/import/security/rollback tests.
- Modify `tests/test_ai_service.py`, `tests/test_ai_service_integration.py`, `tests/test_ai_resident_runtime.py`, `tests/test_ai_tool_management_module.py`, `tests/test_db_connection_service.py`: `package_dir` behavior and fixture updates.
- Modify `tests/test_config_service.py`, `tests/test_config_module.py`, `tests/test_golden_sample_service.py`: import-safe config persistence, UI import/export actions, and restored golden sample behavior.

## Task 1: AI Toolchain `package_dir` Contract

**Files:**
- Modify: `model/ai_toolchain_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/ai_service.py`
- Modify: `app/service/ai_resident_runtime.py`
- Modify: `ui/modules/ai_tool_management.py`
- Test: `tests/test_ai_service.py`
- Test: `tests/test_ai_service_integration.py`
- Test: `tests/test_ai_resident_runtime.py`
- Test: `tests/test_ai_tool_management_module.py`
- Test: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write failing AI service persistence tests**

Add tests in `tests/test_ai_service.py` proving:

```python
def test_save_toolchain_rejects_missing_package_dir(ai_service):
    with pytest.raises(ValueError, match="package_dir"):
        ai_service.save_toolchain(None, {
            "toolchain_id": "tc",
            "display_name": "TC",
            "entrypoint": "python main.py",
            "default_timeout_ms": 30000,
            "package_dir": "",
        })


def test_save_toolchain_rejects_non_directory_package_dir(ai_service, tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="package_dir"):
        ai_service.save_toolchain(None, {
            "toolchain_id": "tc",
            "display_name": "TC",
            "entrypoint": "python main.py",
            "default_timeout_ms": 30000,
            "package_dir": str(missing),
        })


def test_save_toolchain_round_trips_package_dir(ai_service, tmp_path):
    package_dir = tmp_path / "tool"
    package_dir.mkdir()
    saved_id = ai_service.save_toolchain(None, {
        "toolchain_id": "tc",
        "display_name": "TC",
        "entrypoint": "python main.py",
        "default_timeout_ms": 30000,
        "package_dir": str(package_dir),
    })
    row = ai_service.get_toolchain(saved_id)
    assert row.package_dir == str(package_dir)
    assert ai_service.list_toolchains()[0]["package_dir"] == str(package_dir)
```

- [ ] **Step 2: Write failing runtime `cwd` tests**

Add tests proving oneshot and resident execution use `package_dir` as subprocess working directory:

```python
def test_execute_toolchain_runs_with_package_dir_cwd(ai_service, tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai_service.execute_toolchain(
        entrypoint="python main.py",
        package_dir=str(package_dir),
        request_json_path="request.json",
        response_json_path="response.json",
        artifacts_dir="artifacts",
        timeout_ms=30000,
    )
    assert captured["cwd"] == str(package_dir)
```

For `ResidentToolchainRuntime`, update the test helper to call `analyze(..., package_dir=str(package_dir))` and assert `subprocess.Popen(..., cwd=str(package_dir))` is used. Also prove changing `package_dir` restarts the resident process cache.

- [ ] **Step 3: Write failing AI management UI tests**

In `tests/test_ai_tool_management_module.py`, assert the module renders an `AI 包目录` editor, browse button if used, includes `package_dir` in `_collect_save_payload()`, and rejects empty package directories before calling service.

- [ ] **Step 4: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_ai_service.py tests/test_ai_service_preflight.py tests/test_ai_service_validation.py tests/test_ai_resident_runtime.py tests/test_ai_tool_management_module.py tests/test_ai_toolchain_table.py tests/test_ai_binding_table.py tests/test_db_connection_service.py -q
```

Expected: FAIL because `package_dir` does not exist and runtime calls do not accept/use `cwd`.

- [ ] **Step 5: Implement schema, service, runtime, and UI changes**

Implementation requirements:

- Add `package_dir = TextField()` to `AiToolchainTable`.
- In `DbConnectionService._migrate()`, add `("package_dir", "TEXT NOT NULL DEFAULT ''")` to the `ai_toolchain_table` `_ensure_columns()` call. Do not add a release migration for old rows beyond this local development repair.
- In `AiService._normalize_toolchain_payload()`, normalize `package_dir` with `os.path.abspath()` but preserve the local path spelling after trimming; reject missing, non-string, empty, or non-directory values with a clear `ValueError`.
- Save `package_dir` on create/update/rename paths.
- In `preflight_check()`, return `preflight_error` when the bound toolchain `package_dir` is missing or no longer a directory.
- Change `execute_toolchain()` signature to include `package_dir: str`; call `subprocess.run(..., cwd=package_dir, ...)`.
- Change `_execute_ai_transport()` to pass `toolchain.package_dir` to both oneshot and resident runtime.
- Change `ResidentToolchainRuntime.analyze()` and its internal cache identity to include `package_dir`; `subprocess.Popen(..., cwd=package_dir, ...)`; restart cached resident process when either `entrypoint` or `package_dir` changes.
- In `AiToolManagementModule`, add a `QLineEdit` for `package_dir` and optionally a small `浏览` button using `QFileDialog.getExistingDirectory`. Include `package_dir` in draft payload, snapshot, dirty tracking, collection, validation, save payload, and load.
- Keep existing AI request/response JSON protocol unchanged.

- [ ] **Step 6: Update adjacent fixtures**

Update only tests/fixtures that create `AiToolchainTable` rows or call `save_toolchain()` so they provide a temporary package directory. Avoid broad unrelated test rewrites.

- [ ] **Step 7: Run focused tests**

Run:

```powershell
pytest tests/test_ai_service.py tests/test_ai_service_preflight.py tests/test_ai_service_validation.py tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_ai_tool_management_module.py tests/test_ai_toolchain_table.py tests/test_ai_binding_table.py tests/test_db_connection_service.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add model/ai_toolchain_table.py app/service/db_connection_service.py app/service/ai_service.py app/service/ai_resident_runtime.py ui/modules/ai_tool_management.py tests/test_ai_service.py tests/test_ai_service_preflight.py tests/test_ai_service_validation.py tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_ai_tool_management_module.py tests/test_ai_toolchain_table.py tests/test_ai_binding_table.py tests/test_db_connection_service.py
git commit -m "feat(ai): require toolchain package directories"
```

- [ ] **Step 9: Paired review gate**

Dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for this task's git range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Task 2: Config And Golden Sample Import Boundaries

**Files:**
- Modify: `app/service/config_service.py`
- Modify: `app/service/golden_sample_service.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_golden_sample_service.py`

- [ ] **Step 1: Write failing config import tests**

Add tests in `tests/test_config_service.py` proving:

```python
def test_reserve_import_config_id_returns_unused_local_id(config_service):
    reserved_id = config_service.reserve_import_config_id()
    assert isinstance(reserved_id, int)
    assert reserved_id > 0


def test_create_imported_config_uses_reserved_id_and_clears_plc_id(config_service):
    source = _make_payload(config_id=999, config_name="Imported", plc_id=123)
    reserved_id = config_service.reserve_import_config_id()
    new_id = config_service.create_imported_config(
        source,
        reserved_config_id=reserved_id,
    )
    loaded = config_service.load_config(new_id)
    assert new_id == reserved_id
    assert new_id != 999
    assert loaded["config_name"] == "Imported"
    assert loaded["plc_id"] is None


def test_overwrite_imported_config_keeps_local_id_and_clears_plc_id(config_service):
    local_id = config_service.create_config("Imported")
    source = _make_payload(config_id=999, config_name="Imported", plc_id=123)
    saved_id = config_service.overwrite_imported_config(local_id, source)
    assert saved_id == local_id
    assert config_service.load_config(local_id)["plc_id"] is None
```

Add a test proving imported channels with omitted golden assets clear:

```python
for key in _GOLDEN_BINDING_FIELDS:
    assert loaded["channels"][0][key] is None
assert loaded["channels"][0]["fft_golden_mode"] == "off"
assert loaded["channels"][0]["fft_show_golden_overlay"] is False
assert loaded["channels"][0]["oct_golden_mode"] == "off"
assert loaded["channels"][0]["oct_show_golden_overlay"] is False
```

- [ ] **Step 2: Write failing golden sample restore tests**

Add tests in `tests/test_golden_sample_service.py` proving a helper can copy a package-restored WAV into the managed committed directory for `config_id` / `channel_index` and returns metadata that passes `validate_committed_binding()`.

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_config_service.py tests/test_golden_sample_service.py -k "imported_config or reserve_import or restore_imported" -q
```

Expected: FAIL because import helper APIs do not exist.

- [ ] **Step 4: Implement import-safe config helpers**

Implementation requirements:

- Add `ConfigService.reserve_import_config_id() -> int`.
- Add `ConfigService.create_imported_config(payload: dict, *, reserved_config_id: int) -> int`.
- Add `ConfigService.overwrite_imported_config(config_id: int, payload: dict) -> int`.
- Both methods must deep-copy/normalize the input payload, force `plc_id = None`, remove authoritative source timestamps, validate through existing `validate()`, and persist through the same table fields as `begin_save_config()` / `save_config()`.
- `reserve_import_config_id()` must provide an unused local `config_id` so import planning can determine final managed asset paths before import DB writes begin.
- For create, use the previously reserved local `config_id` when inserting the imported config row. If the save fails, remove any newly promoted/imported assets and leave no partially imported config row behind.
- For overwrite, keep the local `config_id` and replace channels exactly like normal save.
- Add a small helper such as `ConfigService.prepare_import_payload(payload, golden_asset_paths_by_channel=None)` that clears golden fields/modes for channels without restored assets and preserves imported input/output hardware text fields.
- Do not change hardware selector/UI code.

- [ ] **Step 5: Implement golden sample restore helper**

Implementation requirements:

- Add `GoldenSampleService.restore_imported_sample(config_id: int, channel_index: int, source_path: str, metadata: dict) -> dict`.
- Copy the WAV into the same managed committed root pattern as existing promoted samples: `data/golden_samples/config_<config_id>/channel_<channel_index>_<uuid>.wav`.
- Return binding metadata with the copied absolute path and imported metadata fields.
- Validate with `validate_committed_binding()` after copy; on failure remove the copied file and raise `ValueError`.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
pytest tests/test_config_service.py tests/test_golden_sample_service.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/service/config_service.py app/service/golden_sample_service.py tests/test_config_service.py tests/test_golden_sample_service.py
git commit -m "feat(config): add import-safe config persistence"
```

- [ ] **Step 8: Paired review gate**

Dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for this task's git range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Task 3: `.dy` Package Export And Validation

**Files:**
- Create: `app/service/config_package_service.py`
- Modify: `app/services_container.py`
- Test: `tests/test_config_package_service.py`

- [ ] **Step 1: Write failing export tests**

Create `tests/test_config_package_service.py` with tests proving:

```python
def test_export_writes_manifest_and_single_config_payload(tmp_path, services):
    target = tmp_path / "export.dy"
    service = services.config_package_service
    service.export_package(config_id=1, target_path=str(target), options=ConfigPackageExportOptions())
    with zipfile.ZipFile(target) as zf:
        assert "manifest.json" in zf.namelist()
        assert "config/config.json" in zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "jingcheng-config-package"
    assert manifest["format_version"] == 1
    assert manifest["app"] == "JingChengMechanical"
    assert isinstance(manifest["exported_at"], str)
    assert manifest["contents"]["config"] is True
```

Add export tests for:

- golden sample inclusion when selected
- AI toolchain metadata inclusion when selected
- AI package file inclusion when selected
- `include_ai_package=True` requires `include_ai_toolchain=True`
- invalid/missing bound toolchain and missing package directory reject export
- missing selected golden sample file rejects export

- [ ] **Step 2: Write failing ZIP safety tests**

Add tests proving export rejects unsafe AI package directories:

- symlink/junction/reparse entries where supported by platform
- duplicate normalized archive names
- case-insensitive path collisions on Windows
- path length and file count limits
- total uncompressed payload larger than `MAX_UNCOMPRESSED_BYTES`; use a monkeypatched low limit or fake file stat so the test does not create a huge file
- more than `MAX_FILE_COUNT` regular file entries; use a monkeypatched low limit for the export unit test

Add a service-level helper test for import validation rejecting:

- non-ZIP file
- missing `manifest.json`
- unsupported format/version
- absolute paths
- drive-prefixed paths
- `..` traversal
- duplicate normalized entry names
- `contents.config` not exactly `true`
- missing `config/config.json`
- extra files under `config/` other than `config/config.json`
- ZIP symlink-like entries, special-file entries, or exposed external-attribute metadata that indicates non-regular files
- manifest-declared golden samples, AI toolchain metadata, or AI package contents that are missing from the archive
- total declared uncompressed payload larger than `MAX_UNCOMPRESSED_BYTES`; use generated `ZipInfo.file_size` values or a monkeypatched low limit instead of writing a huge archive
- more than `MAX_FILE_COUNT` entries; use a monkeypatched low limit for the import validation unit test
- contradictory AI manifest states

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_config_package_service.py -q
```

Expected: FAIL because `ConfigPackageService` does not exist.

- [ ] **Step 4: Implement export and archive validation**

Implementation requirements:

- Define dataclasses:

```python
@dataclass(frozen=True)
class ConfigPackageExportOptions:
    include_golden_samples: bool = False
    include_ai_toolchain: bool = False
    include_ai_package: bool = False


@dataclass(frozen=True)
class ConfigPackagePreview:
    config_name: str
    contents: dict
    config_conflict: bool
    ai_toolchain_id: str | None
    ai_conflict: bool
    config_action_options: tuple[str, ...]
    default_config_action: str
    ai_action_options: tuple[str, ...]
    default_ai_action: str | None
    warnings: tuple[str, ...] = ()
```

- Add constants:

```python
FORMAT = "jingcheng-config-package"
FORMAT_VERSION = 1
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILE_COUNT = 50_000
MAX_NORMALIZED_PATH_LENGTH_WINDOWS = 240
```

- `export_package()` writes to a temporary file in the target directory and atomically replaces the target on success where practical.
- Always write `manifest.json` and `config/config.json`.
- Manifest must include `format`, `format_version`, `app`, `exported_at`, and `contents`.
- Build config payload from `ConfigService.load_config(config_id)` plus `AiService.resolve_binding(config_id)`.
- For golden samples, include only committed existing WAV files, write them as `golden_samples/channel_<channel_index>.wav`, and add `golden_sample_assets`.
- For AI metadata, serialize `ai/toolchain.json` with exactly `toolchain_id`, `display_name`, `version`, `entrypoint`, `protocol_version`, `execution_mode`, `default_timeout_ms`, `enabled`, `description`, diagnostic-only `source_package_dir`, and optional `package_archive_path` when package files are included. Do not serialize local persisted `package_dir` as a package field.
- For AI package export, resolve `package_dir` with `os.path.realpath()`, require it to be a directory, walk it without following symlinks/reparse points, validate real paths stay under root, and write entries under `ai/package/`.
- Validate entry names before writing and before reading. Normalize to forward slashes; reject empty parts, absolute paths, drive prefixes, `..`, duplicates, and Windows case-insensitive collisions.
- On import validation, reject ZIP entries that look like symlinks or special files through `ZipInfo.external_attr` / platform metadata when available.
- Validate manifest-declared contents against actual archive entries: `golden_samples`, `ai/toolchain.json`, and files under exact `package_archive_path/` must exist when declared.
- Enforce the same size/count limits on both export and import: maximum total uncompressed payload `MAX_UNCOMPRESSED_BYTES`, maximum file count `MAX_FILE_COUNT`, and Windows normalized path length `MAX_NORMALIZED_PATH_LENGTH_WINDOWS`.
- Implement an `inspect_package(path) -> ConfigPackagePreview` method that validates manifest/layout and reports conflicts without mutating local state.

- [ ] **Step 5: Register service**

In `app/services_container.py`, instantiate:

```python
self.config_package_service = ConfigPackageService(
    config_service=self.config_service,
    ai_service=self.ai_service,
)
```

If the service also needs golden sample access, get it through `config_service.golden_sample_service` instead of creating a second root.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
pytest tests/test_config_package_service.py tests/test_service_container.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add app/service/config_package_service.py app/services_container.py tests/test_config_package_service.py tests/test_service_container.py
git commit -m "feat(config): export dy packages"
```

- [ ] **Step 8: Paired review gate**

Dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for this task's git range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Task 4: `.dy` Import Execution, Conflicts, Assets, And Rollback

**Files:**
- Modify: `app/service/config_package_service.py`
- Modify: `app/service/ai_service.py`
- Test: `tests/test_config_package_service.py`
- Test: `tests/test_ai_service.py`

- [ ] **Step 1: Write failing import tests**

Add service tests proving:

- Import creates a new config by default and never reuses source `config_id`.
- Config name conflict with `使用旧配置` skips the entire package import and mutates nothing.
- Config name conflict with `覆盖` keeps the local row id and replaces config/channel data.
- Config overwrite with existing managed golden samples does not delete old golden sample files until the replacement config save commits successfully.
- Config overwrite rollback preserves old managed golden sample files and leaves the original DB binding valid when replacement save/import fails before commit.
- `取消` aborts with no mutation.
- Golden sample assets restore into local managed root and channel bindings are rewritten.
- Omitted golden sample assets clear golden bindings and modes.
- PLC binding is always cleared.
- Input/output hardware name, host API, and sample-rate fields are preserved.

Use a decision dataclass such as:

```python
@dataclass(frozen=True)
class ConfigPackageImportDecision:
    config_action: Literal["create", "overwrite", "use_existing_config", "cancel"] = "create"
    ai_action: Literal["create", "overwrite", "use_existing_toolchain", "clear_binding", "cancel"] = "create"


@dataclass(frozen=True)
class ConfigPackageImportResult:
    status: Literal["created", "overwritten", "kept_existing", "cancelled"]
    config_id: int | None
    config_name: str
    ai_binding_status: Literal[
        "created_toolchain",
        "overwrote_toolchain",
        "used_existing_toolchain",
        "cleared_binding",
        "no_ai_metadata",
        "cancelled",
        "unchanged",
    ]
    warnings: tuple[str, ...] = ()
    info_message: str | None = None
```

- [ ] **Step 2: Write failing AI import tests**

Add tests proving:

- Runnable package with no local toolchain creates a toolchain and restores `package_dir` under `data/ai_toolchains/<sanitized>_<hash>/`.
- Runnable package with local conflict and `覆盖` overwrites metadata and managed package.
- Runnable package with local conflict and `使用旧工具链` keeps local metadata/package and binds the imported config to it.
- Metadata-only package with matching local toolchain offers/uses `使用旧工具链`.
- Metadata-only package with matching local toolchain defaults to `使用旧工具链` when building the import decision/preview.
- Metadata-only package without matching local toolchain clears AI binding and returns a warning.
- Metadata-only package never creates or overwrites a toolchain.
- Package with no `ai/toolchain.json` clears imported AI binding, does not create an AI conflict action, and does not mutate existing local toolchains.
- Overwriting an existing managed AI package backs it up and restores it on database failure.
- Overwriting a local external `package_dir` points the toolchain to the new managed package but does not delete the external directory.
- Managed AI package directory with the computed sanitized/hash name and a marker for the same original `toolchain_id` is reused for that toolchain.
- Managed AI package directory collision with a different marker uses deterministic `_2`, `_3`, etc. suffixes instead of reusing the existing directory.

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_config_package_service.py tests/test_ai_service.py -k "import or package_dir" -q
```

Expected: FAIL because import execution is not implemented.

- [ ] **Step 4: Implement import planning and decisions**

Implementation requirements:

- Add `import_package(path, decision) -> ConfigPackageImportResult`.
- Read and validate the `.dy` into a temporary extraction directory.
- Build all conflict decisions before mutating local state.
- Detect config conflict by `config_name` only.
- Detect AI toolchain conflict by `toolchain_id` only.
- `inspect_package()` must return enough information for UI and defaults:
  - `config_action_options` and `default_config_action`
  - `ai_action_options` and `default_ai_action`
  - warnings for metadata-only/no-toolchain cases
  - included-content booleans for config/golden samples/AI metadata/AI package
- Implement exact spec actions:
  - config `overwrite`
  - config `use_existing_config`
  - config `cancel`
  - AI runnable `create` / `overwrite` / `use_existing_toolchain` / `cancel`
  - AI metadata-only `use_existing_toolchain` / `clear_binding` / `cancel`
- When AI metadata is present without AI package files and a matching local toolchain exists, the preview/default decision must select `use_existing_toolchain` by default.
- When the package contains no AI toolchain metadata, clear the imported config AI binding, do not expose an AI conflict action, and do not mutate any AI toolchain row.
- Do not create/overwrite AI toolchain rows from metadata-only packages.
- `import_package()` must return statuses that let the UI distinguish:
  - imported new config
  - overwrote existing config
  - kept existing config and skipped package processing
  - cancelled before mutation
  - whether AI binding used existing toolchain, was cleared, or had no AI metadata

- [ ] **Step 5: Implement managed AI package storage**

Implementation requirements:

- Managed root: `data/ai_toolchains`.
- Directory naming: `<sanitized_toolchain_id>_<sha256_16>` with deterministic numeric suffix for marker mismatch.
- Marker file records original `toolchain_id`.
- If the computed directory already exists with a marker for the same original `toolchain_id`, reuse that directory as the managed package path and apply normal overwrite/backup behavior.
- Add tests that pre-create a managed directory with the computed sanitized/hash name but a different marker; import must choose `_2`, and if `_2` is also occupied by a different marker, choose `_3`.
- Extraction must reject paths escaping target directory and must not execute contents.
- For overwrite, move existing managed package to a backup path first, extract/copy new content to final path, restore backup on failure, delete backup after successful DB commit.
- Do not delete package directories outside managed root.

- [ ] **Step 6: Implement all-or-nothing import asset flow**

Implementation requirements:

- Determine the local `config_id` before restoring golden samples without first committing a config row. For new imports, call `ConfigService.reserve_import_config_id()` during import planning so final managed golden paths can be computed before DB writes.
- Restore golden samples to their final managed paths for the planned local `config_id` before committing config rows that point at them.
- Restore AI package before saving AI toolchain rows that point at it.
- Use one Peewee transaction where possible for config, AI binding, and toolchain DB writes. New-config imports must insert the imported config with the reserved local `config_id` inside this transaction, after asset restoration has succeeded.
- If any DB write fails before commit, remove newly copied assets and restore backups.
- For config overwrite, use the same deferred old golden cleanup semantics as `ConfigService.begin_save_config()` / `commit_pending_save()`: collect old managed golden paths, keep them in place through the DB transaction, delete them only after the replacement config save commits, and preserve them on rollback.
- After DB commit, cleanup backups best-effort with logged warnings.

- [ ] **Step 7: Run focused tests**

Run:

```powershell
pytest tests/test_config_package_service.py tests/test_config_service.py tests/test_ai_service.py tests/test_golden_sample_service.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add app/service/config_package_service.py app/service/ai_service.py tests/test_config_package_service.py tests/test_ai_service.py
git commit -m "feat(config): import dy packages"
```

- [ ] **Step 9: Paired review gate**

Dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for this task's git range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Task 5: Config Page Import / Export UI

**Files:**
- Create: `ui/popups/config_package_dialogs.py`
- Modify: `ui/modules/config.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write failing UI tests**

Add tests proving:

- Config page renders `导入` and `导出` buttons near list actions.
- `导出` warns when no config is selected.
- Export flow opens option collection and calls `config_package_service.export_package()` with `*.dy` target.
- Export options enforce `包含 AI 包` requires `包含 AI 工具链`.
- Import flow opens `*.dy`, calls `inspect_package()`, shows conflict confirmations, then calls `import_package()` with the selected actions.
- Import preview/confirmation text shows the source config name and whether golden samples, AI toolchain metadata, and AI package files are included.
- Successful import refreshes the list and selects the imported/overwritten config.
- Config conflict `使用旧配置` stops without applying assets and shows an informational message.
- Successful import surfaces `ConfigPackageImportResult.warnings` / `info_message`, including the metadata-only/no-local-toolchain warning when AI binding is cleared.
- Metadata-only AI conflict routes `使用旧工具链`, `清空 AI 绑定`, and `取消`.
- Metadata-only AI conflict with a matching local toolchain defaults to `使用旧工具链`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_config_module.py -k "import_export or dy_package" -q
```

Expected: FAIL because buttons/dialog flow does not exist.

- [ ] **Step 3: Implement thin UI wiring**

Implementation requirements:

- Add `self.btn_import = QPushButton("导入")` and `self.btn_export = QPushButton("导出")` in the config list button area.
- Use `QFileDialog.getSaveFileName(..., "DY Packages (*.dy)")` for export and ensure `.dy` suffix if omitted.
- Use `QFileDialog.getOpenFileName(..., "DY Packages (*.dy)")` for import.
- Keep `ui/modules/config.py` thin by moving export/import dialog widgets and decision helpers into `ui/popups/config_package_dialogs.py`. That module owns the popup classes/functions; `ConfigModule` only invokes them and consumes the returned options/decisions. Options:
  - `包含黄金样本`
  - `包含 AI 工具链`
  - `包含 AI 包`
- Disable or auto-uncheck `包含 AI 包` unless `包含 AI 工具链` is checked.
- Show validation errors from `ConfigPackageService` directly in `QMessageBox.warning`.
- After successful `import_package()`, show any returned warnings or informational messages so operators know when AI binding was cleared or an existing config was kept.
- For import conflicts, use one or more `QMessageBox` confirmations. Labels must be:
  - `覆盖`
  - `使用旧配置`
  - `使用旧工具链`
  - `清空 AI 绑定`
  - `取消`
- The import preview/confirmation text must include: source config name, golden sample inclusion, AI toolchain metadata inclusion, and AI package file inclusion.
- For metadata-only AI imports with a matching local toolchain, the default selected/import action must be `使用旧工具链`.
- The dialog text for AI overwrite must clearly warn that overwriting a toolchain may affect other configurations bound to the same `toolchain_id`.
- Do not modify hardware unavailable display logic. Continue relying on `_match_device_combo()`.
- After successful import, call `_refresh_list()` and `_select_config_by_id(result.config_id)` when a config was created/overwritten.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
pytest tests/test_config_module.py tests/test_config_package_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add ui/popups/config_package_dialogs.py ui/modules/config.py tests/test_config_module.py
git commit -m "feat(config): add dy import export UI"
```

- [ ] **Step 6: Paired review gate**

Dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for this task's git range. Proceed only after spec compliance passes and any confirmed current-pass quality defects are fixed or explicitly overruled with concrete technical reasons.

## Task 6: Regression Sweep And Integration Review

**Files:**
- Modify only files needed to fix confirmed regressions from the commands below.
- Test: focused and full suites listed below.

- [ ] **Step 1: Run focused regression suite from spec**

Run:

```powershell
pytest tests/test_config_service.py tests/test_config_module.py tests/test_ai_service.py tests/test_ai_tool_management_module.py tests/test_golden_sample_service.py tests/test_config_package_service.py -q
```

Expected: PASS.

- [ ] **Step 2: Run AI runtime and DB adjacent tests**

Run:

```powershell
pytest tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_db_connection_service.py tests/test_service_container.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full suite**

Run:

```powershell
pytest tests -q
```

Expected: PASS.

- [ ] **Step 4: Fix only confirmed regressions**

If any command fails, inspect the failure and patch only the touched feature boundary unless the failure is clearly from unrelated existing state. Do not modify hardware management code to make import/export tests pass.

- [ ] **Step 5: Commit regression fixes if needed**

```powershell
git add <only-fixed-files>
git commit -m "test(config): stabilize dy import export regressions"
```

- [ ] **Step 6: Paired review gate if regression fixes changed code**

If Step 4 produced a new implementation diff, dispatch `spec-code-reviewer` and `quality-code-reviewer` in parallel for that fix range. If no files changed, record that no task-local review range was produced.

## Final Review

- [ ] Confirm every implementation task above completed its paired `spec-code-reviewer` and `quality-code-reviewer` gate, including documented overrides for any non-actioned findings.
- [ ] Dispatch `quality-code-reviewer` for the full implementation range from the branch base to final head.
- [ ] Confirm final reviewer findings are either fixed or explicitly overruled with concrete technical reasons.
- [ ] Use `verification-before-completion` before reporting completion.
