# Average SPL And Loudness Limit Switches Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional per-bound SPL/loudness trace and average checks, compact SPL/loudness config controls, and 7-row homepage/result summary output without changing FFT/octave/AI behavior.

**Architecture:** Persist the new per-channel enable/value fields through Peewee, config services, and config-package import/export; extend DSP/judgment serialization to compute Leq-based average SPL plus sone-mean average loudness with explicit availability/no-result handling; then update summary/chart/result UI layers to render the new rows and hide disabled limit lines by emitting `null` payload values.

**Tech Stack:** Python, PyQt6, Peewee ORM, NumPy/SciPy, pytest

---

## File Structure

- `model/channel_config_table.py`
  - Add persisted boolean and float fields for SPL/loudness trace and average limit switches/values.
- `app/service/db_connection_service.py`
  - Add migration columns and default-off backfill behavior for the new fields.
- `app/service/config_service.py`
  - Extend defaults, load/save normalization, validation, import preparation, and DB row serialization.
- `app/service/config_package_service.py`
  - Preserve the new fields during export/import round-trips.
- `ui/modules/config.py`
  - Replace separate lower/upper rows with compact same-row checkbox + spinbox controls for SPL/loudness trace and average sections.
- `app/service/analysis/dsp.py`
  - Add a dedicated Leq helper for average SPL and any small reusable helper needed for average loudness reduction.
- `app/service/analysis/frames.py`
  - Extend `ChannelJudgment` with explicit average and availability/error fields.
- `app/service/analysis/judgment.py`
  - Compute and serialize independent SPL/loudness trace + average checks, with unconstrained/default-off and no-result behavior.
- `app/service/analysis/review_payloads.py`
  - Add `spl_average` / `loudness_average` rows, enforce constrained-channel ranking/no-result precedence, and emit `null` chart limits when a bound is disabled.
- `ui/modules/result.py`
  - Expand the fixed result-table rows from 5 to 7.
- `tests/test_db_connection_service.py`
  - Cover schema migration/backfill for the new columns.
- `tests/test_config_service.py`
  - Cover defaults, load/save, validation, and import normalization.
- `tests/test_config_package_service.py`
  - Cover export/import round-trips for new switch/value fields.
- `tests/test_config_module.py`
  - Cover compact control rendering plus collect/populate behavior.
- `tests/test_dsp.py`
  - Cover Leq helper semantics if a new helper is introduced there.
- `tests/test_frames.py`
  - Cover new `ChannelJudgment` serialization fields.
- `tests/test_judgment.py`
  - Cover one-sided bounds, default-off unconstrained checks, average SPL, average loudness, and no-result availability behavior.
- `tests/test_review_payloads.py`
  - Cover 7-row summary output, machine keys, new deviation rules, row no-result precedence, and chart limit `null`s.
- `tests/test_result_module.py`
  - Cover the expanded homepage/result row set.
- `tests/test_test_manager/test_review_payload_emission.py`
  - Update any assertions coupled to serialized `ChannelJudgment` shape or summary row count.

## Task 1: Persist New Config Fields And Package Flow

**Files:**
- Modify: `model/channel_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/config_service.py`
- Modify: `app/service/config_package_service.py`
- Test: `tests/test_db_connection_service.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_config_package_service.py`

- [ ] **Step 1: Write failing persistence and package tests**

Add/extend tests to prove:
- DB migration adds these columns with default-off booleans:
  - `spl_lower_enabled`, `spl_upper_enabled`
  - `spl_average_lower_enabled`, `spl_average_upper_enabled`
  - `loudness_lower_enabled`, `loudness_upper_enabled`
  - `loudness_average_lower_enabled`, `loudness_average_upper_enabled`
- DB migration backfills average numeric columns for existing rows:
  - `spl_average_lower_limit = 40.0`
  - `spl_average_upper_limit = 85.0`
  - `loudness_average_lower_limit = 0.0`
  - `loudness_average_upper_limit = 60.0`
- DB migration preserves existing stored trace-limit numeric values in:
  - `spl_lower_limit`, `spl_upper_limit`
  - `loudness_lower_limit`, `loudness_upper_limit`
- `ConfigService.load_config()` exposes the new booleans and average limit values.
- `ConfigService.save_config()` round-trips the new fields.
- `ConfigService.validate()` ignores disabled bounds, but rejects invalid enabled values/ranges.
- `ConfigService.validate()` rejects every new `*_enabled` field when present but not a boolean.
- `ConfigService.save_config()` normalizes missing or invalid disabled-bound numeric values to safe defaults before writing DB float fields.
- `prepare_import_payload()` backfills missing fields with default-off flags and safe numeric defaults.
- import normalization coerces invalid disabled-bound numeric values to safe defaults before DB writes, not just missing values.
- `ConfigService.validate()` rejects enabled loudness trace/average bounds when their values are negative.
- `ConfigPackageService` export/import preserves all new switch/value fields.

- [ ] **Step 2: Run the focused failing persistence tests**

Run:

```powershell
pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_package_service.py -q
```

Expected: FAIL on missing columns/fields/normalization.

- [ ] **Step 3: Implement schema/default/load-save/import-export support**

Implement:
- Peewee model fields in `model/channel_config_table.py`.
- Migration + backfill in `app/service/db_connection_service.py`.
- `_CHANNEL_DEFAULTS`, `load_config()`, `validate()`, `_normalize_import_channel()`, `_channel_db_values()` in `app/service/config_service.py`.
- Any config-package serialization/import handling needed in `app/service/config_package_service.py`.

Specific defaults:
- all enable flags `False`
- `spl_*_lower_limit = 40.0`
- `spl_*_upper_limit = 85.0`
- `loudness_*_lower_limit = 0.0`
- `loudness_*_upper_limit = 60.0`

- [ ] **Step 4: Re-run the persistence/package tests**

Run:

```powershell
pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_package_service.py -q
```

Expected: PASS

## Task 2: Rebuild SPL/Loudness Config UI With Compact Switch Rows

**Files:**
- Modify: `ui/modules/config.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write failing UI tests for compact controls**

Add/extend tests to prove:
- SPL section now has trace and average rows with object names for enable/value controls.
- Loudness section now has trace and average rows with object names for enable/value controls.
- Populate uses saved values for new booleans and averages.
- Collect returns the new booleans and averages.
- Unchecked bounds remain visible and editable, but do not force validation.
- Changing `loudness_unit` keeps both trace and average numeric values unchanged.

Suggested object names:
- `spl_lower_enabled`, `spl_lower_limit`, `spl_upper_enabled`, `spl_upper_limit`
- `spl_average_lower_enabled`, `spl_average_lower_limit`, `spl_average_upper_enabled`, `spl_average_upper_limit`
- `loudness_lower_enabled`, `loudness_lower_limit`, `loudness_upper_enabled`, `loudness_upper_limit`
- `loudness_average_lower_enabled`, `loudness_average_lower_limit`, `loudness_average_upper_enabled`, `loudness_average_upper_limit`

- [ ] **Step 2: Run the UI test slice**

Run:

```powershell
pytest tests/test_config_module.py -q
```

Expected: FAIL on missing widgets / collect-populate fields.

- [ ] **Step 3: Implement compact UI rows and data collection**

Implement in `ui/modules/config.py`:
- compact same-row lower/upper controls for SPL and loudness trace limits
- new compact same-row rows for average SPL and average loudness
- collect/populate wiring for every new field
- keep unchecked spin boxes enabled and visible
- preserve current `loudness_unit` “values stay as typed” behavior for both trace and average rows

- [ ] **Step 4: Re-run the UI test slice**

Run:

```powershell
pytest tests/test_config_module.py -q
```

Expected: PASS

## Task 3: Add Leq / Average Loudness Judgment And Serialization

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/frames.py`
- Modify: `app/service/analysis/judgment.py`
- Test: `tests/test_dsp.py`
- Test: `tests/test_frames.py`
- Test: `tests/test_judgment.py`

- [ ] **Step 1: Write failing DSP/judgment serialization tests**

Add/extend tests to prove:
- average SPL uses Leq semantics rather than a simple average of trace dB points
- average SPL uses Leq after applying the configured SPL weighting
- average loudness uses arithmetic mean in canonical `sone`
- default-off trace/average groups are unconstrained and pass
- one-sided lower-only and upper-only checks work for SPL and loudness
- `ChannelJudgment` serializes new fields:
  - `spl_average_db`, `spl_average_pass`
  - `spl_trace_available`, `spl_trace_error_message`
  - `spl_average_available`, `spl_average_error_message`
  - `loudness_average_sone`, `loudness_average_pass`
- constrained unavailable trace/average computations serialize `*_pass=None` and make `limit_pass=False`
- constrained unavailable trace/average computations serialize the affected scalar fields as `None`
- constrained unavailable trace/average computations set `JudgmentResult.overall` to `不合格`
- backend-error + all-bounds-disabled paths keep SPL/loudness rows unconstrained and do not fail `limit_pass`

- [ ] **Step 2: Run the DSP/judgment test slice**

Run:

```powershell
pytest tests/test_dsp.py tests/test_frames.py tests/test_judgment.py -q
```

Expected: FAIL on missing helpers/fields/logic.

- [ ] **Step 3: Implement DSP helper and judgment logic**

Implement:
- a dedicated Leq helper in `app/service/analysis/dsp.py` (do not reuse the current chunk trace final-value logic for average SPL)
- apply the configured SPL weighting before Leq calculation
- `ChannelJudgment` field additions in `app/service/analysis/frames.py`
- in `app/service/analysis/judgment.py`:
  - keep SPL scalar and SPL trace computation behavior as today, including trace min/max derivation for result payloads
  - compute average SPL with Leq and independent enable flags
  - compute loudness trace once, then derive max/min plus average sone
  - convert stored trace and average loudness limits from the selected `loudness_unit` into canonical sones before judgment/comparison
  - treat unconstrained groups as `*_pass=True`
  - serialize partial availability separately for `SPL` vs `平均SPL`
  - preserve existing FFT/octave behavior

- [ ] **Step 4: Re-run the DSP/judgment test slice**

Run:

```powershell
pytest tests/test_dsp.py tests/test_frames.py tests/test_judgment.py -q
```

Expected: PASS

## Task 4: Update Summary, Charts, Result Table, And Review Integration

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `ui/modules/result.py`
- Modify: `app/service/analysis/test_manager.py` only if needed for explicit payload compatibility
- Test: `tests/test_review_payloads.py`
- Test: `tests/test_result_module.py`
- Test: `tests/test_test_manager/test_review_payload_emission.py`

- [ ] **Step 1: Write failing summary/result/chart tests**

Add/extend tests to prove:
- summary rows now appear in this exact order:
  - `spl`
  - `spl_average`
  - `loudness`
  - `loudness_average`
  - `fft`
  - `octave`
  - `ai`
- unconstrained rows display `OK / -- / --`
- constrained unavailable rows become `无结果` and take precedence over worst-deviation ranking
- constrained unavailable rows report the first blocking channel in selected-channel order
- constrained unavailable rows make `summary_payload.overall_status` become `no_result` even though `JudgmentResult.limit_pass` is fail-safe `False`
- backend-error + all-bounds-disabled paths keep SPL/loudness rows at `OK / -- / --` and do not change summary `overall_status` to `no_result`
- deviation rules are explicit:
  - one enabled bound: deviation is measured only against that bound
  - two enabled bounds: deviation is measured against the active range
  - exact enabled bound: deviation is `0`
  - out-of-range violation: deviation is positive distance to the violated bound
  - in-range clearance: deviation is negative distance to the nearest enabled bound
  - `status=NG` only when deviation is positive; `status=OK` when deviation is `0` or negative
- average loudness ranking uses canonical `sone` but displays in the selected channel unit
- SPL/loudness chart payload keeps existing keys but uses `null` limit values when a bound is disabled
- `ResultModule` table row count and placeholders expand from 5 to 7
- any test-manager review-payload expectations continue to match `asdict(ChannelJudgment)` output

- [ ] **Step 2: Run the summary/result integration slice**

Run:

```powershell
pytest tests/test_review_payloads.py tests/test_result_module.py tests/test_test_manager/test_review_payload_emission.py -q
```

Expected: FAIL on old row count / old payload assumptions.

- [ ] **Step 3: Implement summary/chart/result changes**

Implement in `app/service/analysis/review_payloads.py`:
- row builders for `平均SPL` and `平均Loudness`
- constrained-channel-only ranking
- no-result precedence using the first blocking selected channel
- existing-key chart payloads with `null` limits when a bound is disabled

Implement in `ui/modules/result.py`:
- expand `_ROW_LABELS`
- resize table height for 7 rows
- keep placeholder / summary rendering logic aligned with new row count

Only touch `app/service/analysis/test_manager.py` if a small compatibility shim is needed for payload consumers.

- [ ] **Step 4: Re-run the summary/result integration slice**

Run:

```powershell
pytest tests/test_review_payloads.py tests/test_result_module.py tests/test_test_manager/test_review_payload_emission.py -q
```

Expected: PASS

## Task 5: Run Broader Affected Verification

**Files:**
- Test only: `tests/test_test_manager`
- Test only: `tests/test_factory_mode_controller`
- Test only: `tests/test_main_window.py`

- [ ] **Step 1: Run the focused full feature verification**

Run:

```powershell
pytest tests/test_config_service.py tests/test_db_connection_service.py tests/test_config_package_service.py -q
pytest tests/test_config_module.py -q
pytest tests/test_dsp.py tests/test_frames.py tests/test_judgment.py -q
pytest tests/test_review_payloads.py tests/test_result_module.py tests/test_test_manager/test_review_payload_emission.py -q
```

Expected: PASS

- [ ] **Step 2: Run the broader affected suite**

Run:

```powershell
pytest tests/test_test_manager tests/test_factory_mode_controller tests/test_main_window.py -q
```

Expected: PASS, or only unrelated pre-existing failures with no code changes made to chase them.

- [ ] **Step 3: Record any residual non-scope failures**

If broader tests fail for unrelated reasons, stop and report the exact failing test names plus why they are outside this change.

- [ ] **Step 4: Do not commit unless explicitly requested**

Leave the branch/worktree ready for review. Do not create commits unless the orchestrator or user explicitly asks for them.
