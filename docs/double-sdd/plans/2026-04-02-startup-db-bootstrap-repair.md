# Startup DB Bootstrap Repair Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make startup fully bootstrap empty and older SQLite databases so runtime UI paths stop failing on missing tables or columns.

**Architecture:** Extend `DbConnectionService` to own the full runtime bootstrap contract: create all required tables, add missing columns for older schemas, and seed required singleton/default rows. Lock the behavior down with targeted DB bootstrap regression tests, including a legacy config load that currently fails on missing golden-sample columns.

**Tech Stack:** Python, Peewee ORM, SQLite, pytest

---

### Task 1: Expand startup DB bootstrap and cover it with regression tests

**Files:**
- Modify: `app/service/db_connection_service.py`
- Modify: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write the failing tests**

Add tests that cover:
- fresh DB bootstrap creates this exact full runtime table set:
  `test_config_table`, `channel_config_table`, `ai_toolchain_table`,
  `test_config_ai_binding_table`, `ai_execution_table`, `device_table`,
  `audio_data_table`, `users_table`, `system_info_table`,
  `training_model_table`
- fresh DB bootstrap seeds `device_table.id = 1`
- fresh DB bootstrap seeds a default admin user when `users_table` is empty:
  `user_name='admin'`, `password=encrypt_password('admin', 'admin')`,
  `access_level='Admin'`, and both timestamp fields populated
- repeated `connect()` calls remain idempotent and do not duplicate seeded rows
- a pre-existing `device_table` without row `id = 1` is repaired by inserting that singleton row
- existing non-empty `device_table` and `users_table` rows are preserved
- legacy `test_config_table` rows missing runtime columns are migrated in place
- legacy `test_config_ai_binding_table` rows gain `score_threshold` while preserving `analysis_window_offset_ms`
- legacy `channel_config_table` rows missing loudness, golden binding, and golden comparison-mode columns are migrated so `ConfigService.load_config()` succeeds
- legacy `ai_toolchain_table` rows gain current runtime columns needed by `AiService`
- legacy `ai_execution_table` rows gain current runtime columns needed by result persistence
- unknown legacy extra columns remain untouched after startup migration
- the seeded admin row can be matched by the same login hash query path used in `LoginWindow`
- `HardwareSelectService.load_io_devices()` succeeds against the repaired `device_table.id = 1` singleton row

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_db_connection_service.py -q`

Expected: FAIL on the new bootstrap regression coverage before implementation.

- [ ] **Step 3: Implement the minimal bootstrap and migration fix**

Update `DbConnectionService` to:
- create the full runtime table set from one shared bootstrap model registry
- add these exact `test_config_table` columns when missing:
  `input_device_name`, `input_device_hostapi`, `input_device_samplerate`,
  `output_device_name`, `output_device_hostapi`,
  `output_device_samplerate`, `selected_channels`, `plc_device_key`,
  `plc_device_label`, `analysis_window_offset_ms`
- add these exact `channel_config_table` columns when missing:
  `loudness_unit`, `loudness_lower_limit`, `loudness_upper_limit`,
  `golden_sample_path`, `golden_sample_label`,
  `golden_sample_source_channel`, `golden_sample_start_sample`,
  `golden_sample_num_samples`, `golden_sample_start_ms`,
  `golden_sample_duration_ms`, `golden_sample_sample_rate`,
  `fft_golden_mode`, `fft_show_golden_overlay`,
  `oct_golden_mode`, `oct_show_golden_overlay`
- keep existing loudness migration behavior
- add missing `test_config_ai_binding_table.score_threshold` without deleting or rebuilding legacy `analysis_window_offset_ms`
- add these exact `ai_toolchain_table` columns when missing:
  `protocol_version`, `execution_mode`, `description`
- add these exact `ai_execution_table` columns when missing:
  `toolchain_name`, `toolchain_version`, `protocol_version`,
  `window_overridden`, `default_window_offset_ms`,
  `default_window_duration_ms`, `final_window_offset_ms`,
  `final_window_duration_ms`, `is_current_effective`,
  `request_json_path`, `response_json_path`, `artifacts_dir`
- seed `device_table.id = 1`
- seed a single default admin only when `users_table` is empty:
  `user_name='admin'`, `password=encrypt_password('admin', 'admin')`,
  `access_level='Admin'`, `user_created_time=now_str()`,
  `user_updated_time=now_str()`
- keep repeated startup calls idempotent

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/test_db_connection_service.py -q`

Expected: PASS

- [ ] **Step 5: Run broader regression verification**

Run: `pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_main_window.py tests/test_service_container.py -q`

Expected: PASS, covering DB bootstrap, config loading, login/main-window flow, and startup service wiring.

- [ ] **Step 6: Commit**

```bash
git add app/service/db_connection_service.py tests/test_db_connection_service.py docs/double-sdd/specs/2026-04-02-startup-db-bootstrap-repair-design.md docs/double-sdd/plans/2026-04-02-startup-db-bootstrap-repair.md
git commit -m "fix(db): bootstrap full runtime schema"
```
