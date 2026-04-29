# Startup DB Bootstrap Repair Design

## Goal

Make application startup bring an empty or older SQLite database all the way to the current runtime schema in one pass so the app no longer crashes later on missing tables or columns.

## Problem

The current `DbConnectionService.connect()` only creates a subset of tables and only migrates a few columns. That leaves multiple valid runtime paths broken:

- empty DBs miss legacy tables such as `device_table`, `audio_data_table`, and `users_table`
- older DBs miss newer `channel_config_table` golden-sample columns
- a DB can appear to boot successfully, then crash later when hardware, login, or config loading touches a missing table or column

## Design

### 1. Bootstrap the full runtime table set

`DbConnectionService.connect()` will define one single-source-of-truth bootstrap model registry inside the service and use that registry for both `create_tables(..., safe=True)` and related bootstrap tests.

The runtime bootstrap registry will contain all model-backed tables used by normal startup and core UI flows:

- `test_config_table`
- `channel_config_table`
- `ai_toolchain_table`
- `test_config_ai_binding_table`
- `ai_execution_table`
- `device_table`
- `audio_data_table`
- `users_table`
- `system_info_table`
- `training_model_table`

This keeps startup aligned with one explicit runtime bootstrap contract instead of separate drifting subsets.

### 2. Run explicit in-place migrations for older DB files

After `create_tables(..., safe=True)`, startup will inspect existing tables with `PRAGMA table_info(...)` and `ALTER TABLE ... ADD COLUMN ...` any missing columns required by current runtime code.

The migration set must include these exact columns:

- `test_config_table`
  - `input_device_name TEXT`
  - `input_device_hostapi TEXT`
  - `input_device_samplerate REAL`
  - `output_device_name TEXT`
  - `output_device_hostapi TEXT`
  - `output_device_samplerate REAL`
  - `selected_channels TEXT DEFAULT '[]'`
  - `plc_device_key TEXT`
  - `plc_device_label TEXT`
  - `analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0`
- `channel_config_table`
  - `loudness_unit TEXT DEFAULT 'sone'`
  - `loudness_lower_limit REAL DEFAULT 0`
  - `loudness_upper_limit REAL DEFAULT 60`
  - `golden_sample_path TEXT DEFAULT NULL`
  - `golden_sample_label TEXT DEFAULT NULL`
  - `golden_sample_source_channel INTEGER DEFAULT NULL`
  - `golden_sample_start_sample INTEGER DEFAULT NULL`
  - `golden_sample_num_samples INTEGER DEFAULT NULL`
  - `golden_sample_start_ms INTEGER DEFAULT NULL`
  - `golden_sample_duration_ms INTEGER DEFAULT NULL`
  - `golden_sample_sample_rate REAL DEFAULT NULL`
  - `fft_golden_mode TEXT DEFAULT 'off'`
  - `fft_show_golden_overlay INTEGER NOT NULL DEFAULT 0`
  - `oct_golden_mode TEXT DEFAULT 'off'`
  - `oct_show_golden_overlay INTEGER NOT NULL DEFAULT 0`
- `test_config_ai_binding_table`
  - `score_threshold REAL DEFAULT NULL`
  - keep legacy `analysis_window_offset_ms` untouched if present
- `ai_toolchain_table`
  - `protocol_version INTEGER NOT NULL DEFAULT 1`
  - `execution_mode TEXT NOT NULL DEFAULT 'oneshot_process'`
  - `description TEXT NOT NULL DEFAULT ''`
- `ai_execution_table`
  - `toolchain_name TEXT`
  - `toolchain_version TEXT`
  - `protocol_version INTEGER NOT NULL DEFAULT 1`
  - `window_overridden INTEGER NOT NULL DEFAULT 0`
  - `default_window_offset_ms INTEGER NOT NULL DEFAULT 0`
  - `default_window_duration_ms INTEGER NOT NULL DEFAULT 0`
  - `final_window_offset_ms INTEGER NOT NULL DEFAULT 0`
  - `final_window_duration_ms INTEGER NOT NULL DEFAULT 0`
  - `is_current_effective INTEGER NOT NULL DEFAULT 0`
  - `request_json_path TEXT`
  - `response_json_path TEXT`
  - `artifacts_dir TEXT`

Unknown legacy extra columns remain untouched.

Startup must also preserve/create the non-column schema object it already owns today:

- index `testconfigaibindingtable_toolchain_id` on `test_config_ai_binding_table(toolchain_id)`

Startup does not take ownership of any undocumented external SQLite triggers. The `users_table` single-admin rule remains enforced by the current application/UI behavior.

### 3. Seed required singleton/default rows

Startup will ensure:

- `device_table` has row `id = 1`
- `users_table` gets one local admin account only when the table is completely empty

Default admin seed:

- username: `admin`
- password: store `encrypt_password('admin', 'admin')`
- role: `Admin`
- `user_created_time` and `user_updated_time` use `now_str()`

This seed only happens for a blank `users_table`, so existing user DBs are unchanged even if they have no admin row.

### 4. Keep migrations idempotent

Repeated calls to `connect()` must be safe:

- no duplicate rows
- no failure when columns already exist
- no deletion or rewrite of existing user/config/audio records

## Testing

Add regression tests that prove:

- fresh DB bootstrap creates the full runtime table set
- fresh DB bootstrap seeds `device_table.id = 1`
- fresh DB bootstrap seeds one admin user in an otherwise empty `users_table`
- repeated `connect()` calls do not duplicate the seeded device row or admin row
- pre-existing non-empty `device_table` and `users_table` rows are preserved
- a pre-existing `device_table` without row `id = 1` is repaired by adding that singleton row
- legacy `channel_config_table` rows missing loudness, golden binding, and golden comparison-mode columns are migrated in place
- older `ai_toolchain_table` rows missing current runtime columns are migrated in place
- older `ai_execution_table` rows missing current runtime columns are migrated in place
- legacy `test_config_ai_binding_table` rows gain `score_threshold` while preserving `analysis_window_offset_ms`
- unknown legacy extra columns remain untouched after startup migration
- after migration, `ConfigService.load_config()` can read an old config without raising `no such column`
- after migration, the login consumer can authenticate against the seeded admin row using the same hash path as `LoginWindow`
- after migration, `HardwareSelectService.load_io_devices()` can read the repaired singleton `device_table.id = 1` row without failure

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: SQLite schema, startup behavior, login flow, hardware page, config loading
- Allowed breakage: no loss of existing persisted rows; unused legacy extra columns may remain
- Migration strategy: in-place startup migration plus singleton/default seeding when required
