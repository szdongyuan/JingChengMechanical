# Audio Data Result And Config Persistence Design

## Summary

Persist the automatic test result label and the test configuration used for each newly recorded audio data row. `audio_data_table.labels` stores the data-management-facing OK/NG label, `audio_data_table.analyse_config` stores the human-readable config name, and a new `audio_data_table.config_content` column stores the full runtime config JSON snapshot for traceability.

This change is intentionally scoped to the recording/analysis save path and database schema. The data management UI and export code are consumers only and must not be modified in this pass. If existing data-management code dynamically renders model/database columns, the new `config_content` column may appear through that existing behavior; no data-management code should be edited to special-case it.

## Goals

- Save `labels` for newly completed automatic analysis records:
  - `JudgmentResult.overall == "合格"` maps to `OK`.
  - `JudgmentResult.overall == "不合格"` maps to `NG`.
- Save `analyse_config` as the config name from the runtime config dict.
  - Source key: `config["config_name"]`.
  - Fallback: stringified `config["config_id"]` when `config_name` is missing/blank.
  - Final fallback: empty string.
- Add and save `config_content` as a JSON snapshot of the runtime config dict used by that test session.
- Keep existing historical `audio_data_table` rows valid. Existing rows may keep `labels`, `analyse_config`, and `config_content` as `NULL`.
- Keep the implementation out of data management UI code.

## Non-Goals

- No manual data management UI, playback, table layout, or export UI changes.
- No backfill for historical audio rows.
- No changes to manual PASS/FAIL buttons.
- No changes to judgment logic.
- No changes to AI request/response protocol.
- No changes to hardware, PLC, or data-management-owned code.

## Current Behavior

`TestSession._save_wav()` writes the captured WAV and creates one `AudioDataTable` row with file path, product model, sample rate, record time, barcode, and `labels=None`.

The final `JudgmentResult` is produced later by `TestSession._run_judgment()`, so `_save_wav()` does not yet know whether the run is OK or NG. The config dict is already available on `TestSession._config`, but it is not persisted into `audio_data_table`.

`DataManagementModule` currently reads columns dynamically from `AudioDataTable`, so the new model column may be visible there automatically. That is acceptable as an existing consumer behavior, but this implementation must not edit data-management code.

## Design

### Schema

Add a nullable text field to `AudioDataTable`:

```text
config_content TEXT NULL
```

Extend startup DB repair in `DbConnectionService._migrate()` so existing local development databases gain both current audio-data metadata columns when missing:

```text
analyse_config TEXT
config_content TEXT
```

`analyse_config` already exists in the current model and may already exist in the local database. The migration remains idempotent.

### Save Flow

Keep `_save_wav()` responsible for creating the row early, before analysis. It should continue to set:

```text
labels = None
analyse_config = config_name if available
config_content = JSON snapshot if serializable
```

After `_run_judgment()` obtains the final `JudgmentResult`, update the same audio row identified by `self._audio_data_id`:

```text
labels = "OK" if overall == "合格"
labels = "NG" if overall == "不合格"
analyse_config = config_name
config_content = config JSON snapshot
```

If `overall` is any unexpected value, leave `labels` unchanged/`NULL` and log a warning. If the result is unavailable or analysis raises before a `JudgmentResult` exists, the row may remain unlabeled. The existing no-result/error paths should not be broadened in this pass.

If `self._audio_data_id` is missing, stale, or the update affects zero rows, log a warning and continue without changing the judgment result or emitted UI signals.

### Config Snapshot

Serialize `TestSession._config` with `json.dumps(..., ensure_ascii=False, sort_keys=True, default=str)`.

If serialization still fails, for example due to a circular reference, store `config_content = None`, continue the test flow, and log a warning. The normal config loaded by `ConfigService.load_config()` is expected to serialize successfully.

The snapshot is a diagnostic/configuration record, not an import/export format. It does not need to be normalized for `.dy` migration.

### Error Handling

Result metadata persistence is best-effort after the WAV row exists:

- Failure to update label/config metadata must not delete the WAV file.
- Failure to update label/config metadata must not change the judgment result or UI signals.
- Log a warning through the existing bus if the metadata update fails.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces: data management UI code, data management playback/export behavior, judgment behavior, AI protocol, hardware/PLC modules.
- Allowed breakage: none for runtime startup. Existing rows may have `NULL` in the new/current metadata columns.
- Migration strategy: startup schema repair adds missing nullable audio metadata columns. No historical row backfill.

## Testing Strategy

- Unit test `TestSession._save_wav()` creates rows with `analyse_config` and `config_content`.
- Unit test successful `_run_judgment()` updates the existing row to `labels="OK"` for `"合格"` and `labels="NG"` for `"不合格"`.
- Unit test config snapshot contains the config name and selected channels.
- DB bootstrap test verifies `audio_data_table.config_content` is created for fresh and migrated databases.

Focused verification:

```powershell
pytest tests/test_test_session.py tests/test_db_connection_service.py -q
```
