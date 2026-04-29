# Average SPL And Loudness Limit Switches Design

## Purpose

The current SPL and loudness limit checks judge the full time trace against required lower and upper values. Operators need more flexible limit rules:

- SPL trace, average SPL, loudness trace, and average loudness are separate judgment items.
- Lower and upper bounds are independently optional for every judgment item.
- All SPL/loudness bound switches default to off; a bound only participates in validation, judgment, and chart limit display after the operator checks it.
- Average checks appear in the home/result summary table.
- The channel configuration UI is more compact, with lower and upper controls placed on the same row.

For user-facing wording, the SPL average is displayed as average SPL / average sound pressure level. Internally it is computed as equivalent continuous sound level, Leq.

## Scope

In scope:

- Add persisted per-channel enable flags for SPL lower/upper, average SPL lower/upper, loudness lower/upper, and average loudness lower/upper.
- Add persisted per-channel lower/upper values for average SPL and average loudness.
- Load, validate, save, import, and export the new fields through existing config services and package flows.
- Update the configuration UI so all affected lower/upper rows include independent enable switches and compact same-row layout.
- Update judgment so each enabled bound is enforced independently.
- Add independent average SPL and average loudness pass/fail fields to judgment serialization.
- Add average SPL and average loudness rows to the home/result summary table.
- Preserve SPL and loudness chart algorithms; chart payloads include each existing line limit only when that specific bound is enabled.
- Add focused tests for config defaults/migration, validation, judgment, summary payloads, and result-table rendering.

Out of scope:

- Replacing the existing SPL or loudness chart algorithms.
- Adding average lines to charts.
- Changing FFT, octave, AI, STFT, or golden-sample behavior.
- Changing result persistence schemas outside the already serialized judgment/summary payloads.
- Building a generic configurable rule engine.

## Approved Decisions

1. Existing SPL and loudness trace checks remain as separate checks.
2. Average SPL and average loudness are new independent checks.
3. All four check groups support independently enabled lower and upper bounds.
4. All lower/upper bound switches default to off for new rows, migrated rows, missing config fields, and imported payloads.
5. If both bounds are disabled for a check group, that check group is unconstrained and passes without affecting the verdict.
6. Overall limit pass requires every enabled check group and all existing FFT/octave checks to pass.
7. Average SPL uses Leq internally, but the UI and result table can say average SPL / average sound pressure level.
8. Average loudness uses the arithmetic mean of the loudness trace in sones for canonical judgment.
9. Loudness display and input units continue to support `sone` and `phon`; canonical comparison uses sones.
10. Backward compatibility with old SPL/loudness limit behavior is not required because this is a new project.

## Data Model

Extend `channel_config_table` with these columns:

- `spl_lower_enabled` boolean, default false
- `spl_upper_enabled` boolean, default false
- `spl_average_lower_enabled` boolean, default false
- `spl_average_lower_limit` real, default 40.0
- `spl_average_upper_enabled` boolean, default false
- `spl_average_upper_limit` real, default 85.0
- `loudness_lower_enabled` boolean, default false
- `loudness_upper_enabled` boolean, default false
- `loudness_average_lower_enabled` boolean, default false
- `loudness_average_lower_limit` real, default 0.0
- `loudness_average_upper_enabled` boolean, default false
- `loudness_average_upper_limit` real, default 60.0

The existing value columns remain:

- `spl_lower_limit`
- `spl_upper_limit`
- `loudness_lower_limit`
- `loudness_upper_limit`

Loudness limit storage rule:

- `loudness_lower_limit`, `loudness_upper_limit`, `loudness_average_lower_limit`, and `loudness_average_upper_limit` are stored in the channel's selected `loudness_unit` exactly as entered in the UI.
- Canonical conversion into sones happens only during judgment, comparison, and cross-channel ranking.
- When the operator changes `loudness_unit`, the existing trace and average loudness numeric limit values remain visible as-is; they are not converted and not reset.

`ConfigService.load_config()` exposes all new fields. Save/import normalization supplies defaults for missing fields, with all enable flags defaulting to false.

## Judgment Contract

The in-memory `ChannelJudgment` contract is authoritative and must be expanded before serialization so `asdict()` produces this explicit result shape:

- `channel_index: int`
- `spl_db: float | None`
- `spl_pass: bool | None`
- `spl_min_db: float | None`
- `spl_max_db: float | None`
- `spl_average_db: float | None`
- `spl_average_pass: bool | None`
- `spl_trace_available: bool`
- `spl_trace_error_message: str | None`
- `spl_average_available: bool`
- `spl_average_error_message: str | None`
- `loudness_min_sone: float | None`
- `loudness_max_sone: float | None`
- `loudness_average_sone: float | None`
- `loudness_pass: bool | None`
- `loudness_average_pass: bool | None`
- `loudness_available: bool`
- `loudness_error_message: str | None`
- `fft_available: bool`
- `fft_error_message: str | None`
- `octave_available: bool`
- `octave_error_message: str | None`
- `fft_band_results: list[BandResult]`
- `oct_band_results: list[BandResult]`

Serialization contract for pass fields:

- If a check group is unconstrained because both bounds are disabled, its `*_pass` field is serialized as `True`.
- If a constrained check group has a valid computed value, its `*_pass` field is serialized as `True` or `False`.
- If a constrained check group cannot be computed, its `*_pass` field is serialized as `None`, and its scalar/trace-derived result fields for that check group are serialized as `None`.

## Judgment Behavior

For each selected channel:

1. Compute calibrated signal as today.
2. Compute SPL scalar and SPL trace as today.
3. Compute SPL trace min/max for display and trace-bound deviation.
4. Compute average SPL as Leq over the calibrated analysis signal using the configured SPL weighting.
5. Compute loudness trace as today when at least one loudness result, loudness chart, or loudness check needs it.
6. Compute loudness trace min/max for display and trace-bound deviation.
7. Compute average loudness as the arithmetic mean of the loudness trace in sones.
8. Evaluate each check group using only enabled bounds.

SPL trace check:

- Lower failure only applies when `spl_lower_enabled` is true.
- Upper failure only applies when `spl_upper_enabled` is true.
- When both are false, `spl_pass` is true and the summary row is unconstrained.

Average SPL check:

- Lower failure only applies when `spl_average_lower_enabled` is true.
- Upper failure only applies when `spl_average_upper_enabled` is true.
- When both are false, `spl_average_pass` is true and the summary row is unconstrained.

Loudness trace check:

- Lower failure only applies when `loudness_lower_enabled` is true.
- Upper failure only applies when `loudness_upper_enabled` is true.
- When both are false, `loudness_pass` is true and the summary row is unconstrained.

Average loudness check:

- Lower failure only applies when `loudness_average_lower_enabled` is true.
- Upper failure only applies when `loudness_average_upper_enabled` is true.
- When both are false, `loudness_average_pass` is true and the summary row is unconstrained.

Loudness backend errors:

- If any loudness trace or average loudness bound is enabled, loudness backend errors make the affected constrained loudness row no-result and make the overall limit result fail.
- If all loudness trace and average loudness bounds are disabled, loudness backend errors do not affect the overall limit result; the unconstrained loudness rows display `OK / -- / --`.
- Chart rendering may still omit the loudness chart when the backend is unavailable.

SPL backend errors:

- `spl_trace_available` / `spl_trace_error_message` describe only the SPL trace computation used by the `SPL` row and the SPL chart.
- `spl_average_available` / `spl_average_error_message` describe only the Leq computation used by the `平均SPL` row.
- If a constrained SPL trace computation is unavailable, the `SPL` row becomes no-result and the overall limit result fails.
- If a constrained average SPL computation is unavailable, the `平均SPL` row becomes no-result and the overall limit result fails.
- If all SPL trace bounds are disabled, SPL trace backend errors do not affect the overall limit result; the unconstrained `SPL` row displays `OK / -- / --`.
- If all average SPL bounds are disabled, average SPL backend errors do not affect the overall limit result; the unconstrained `平均SPL` row displays `OK / -- / --`.
- Chart rendering may omit the SPL chart when `spl_trace_available` is false.

No-result behavior:

- A summary row is `no-result` only when at least one selected channel has that check group constrained but the required computed value is unavailable.
- No-result takes precedence over deviation ranking. If any constrained channel blocks the row, the row is `无结果` and worst-deviation ranking is skipped for that row.
- A no-result row displays status `无结果`, deviation `--`, and the first blocking channel in selected-channel order; otherwise it displays channel `--`.
- A no-result row is treated as blocking for overall summary status.
- Availability from channels where that row is unconstrained is ignored for that row. For example, if `SPL` trace bounds are disabled for a channel, `spl_trace_available` does not change the `SPL` row for that channel.

## Summary Table

Update summary rows from five to seven rows:

1. `item_key=spl`, label `SPL`
2. `item_key=spl_average`, label `平均SPL`
3. `item_key=loudness`, label `Loudness`
4. `item_key=loudness_average`, label `平均Loudness`
5. `item_key=fft`, label `FFT`
6. `item_key=octave`, label `倍频程`
7. `item_key=ai`, label `AI分析`

Each summary row uses the worst configured channel by deviation, following the existing row behavior. Rows with no enabled bounds are unconstrained and display:

- status `OK`
- deviation `--`
- channel `--`

Channel ranking scope:

- A row ranks only channels where that row is constrained for that channel.
- Channels where both bounds are disabled for that row are excluded from ranking for that row.
- If every selected channel is unconstrained for that row, the row is unconstrained.

Deviation contract:

- If one bound is enabled, deviation is measured only against that bound.
- If both bounds are enabled, deviation is measured against the active range.
- Out-of-range deviation is a positive distance to the violated bound.
- In-range deviation is a negative clearance to the nearest enabled bound.
- On the exact enabled bound, deviation is `0`.
- `status` is `NG` only when deviation is positive; `OK` when deviation is `0` or negative.

For average loudness rows, cross-channel ranking uses canonical sone deviation. The displayed deviation is recomputed in the selected channel's configured loudness unit.

Overall-status contract across surfaces:

- `JudgmentResult.limit_pass` is the machine pass/fail gate for factory logic. Any constrained no-result condition sets `limit_pass = False`.
- `JudgmentResult.overall` therefore becomes `不合格` when a constrained computation is unavailable.
- `summary_payload.overall_status` is the UI summary state and becomes `no_result` when any row is `无结果`.
- This difference is intentional: the machine verdict is fail-safe, while the UI distinguishes hard failures from unavailable required analysis.

## Chart Payload Contract

Existing chart payload keys stay stable.

For SPL charts:

- `lower_limit_db` is a numeric value only when `spl_lower_enabled` is true; otherwise it is `null`.
- `upper_limit_db` is a numeric value only when `spl_upper_enabled` is true; otherwise it is `null`.

For loudness charts:

- `lower_limit` is a numeric value only when `loudness_lower_enabled` is true; otherwise it is `null`.
- `upper_limit` is a numeric value only when `loudness_upper_enabled` is true; otherwise it is `null`.

Disabled bounds are therefore omitted from rendering by emitting `null` in the existing payload slots rather than by removing keys.

## Configuration UI

Update the SPL and loudness channel sections:

- Keep the current SPL weighting and time constant controls.
- For SPL trace limits, show lower and upper controls on one row with independent checkboxes.
- For average SPL limits, show lower and upper controls on one row with independent checkboxes.
- Keep the current loudness unit control.
- For loudness trace limits, show lower and upper controls on one row with independent checkboxes.
- For average loudness limits, show lower and upper controls on one row with independent checkboxes.

The numeric spin boxes remain visible and editable even when their checkbox is off so operators can prefill values before enabling the bound. Unchecked bounds do not participate in validation, judgment, or chart limit line display.

Labels should be concise and fit the compact layout:

- `SPL`
- `平均SPL`
- `响度`
- `平均响度`

## Validation

Validation rules:

- Enable flags must be booleans when present.
- Numeric limit values must be numeric when their corresponding bound is enabled.
- If both lower and upper are enabled for the same check group, lower must be less than upper.
- Disabled bound values may still be present; they are ignored by judgment.
- Save/import normalization should coerce or backfill missing/invalid disabled bound values to safe numeric defaults before writing database float fields.
- Loudness values must be non-negative when the corresponding bound is enabled.
- SPL limits keep the existing numeric range expectations in the UI.

For compatibility with existing UI save paths that always send numeric values, save continues to include numeric values for disabled bounds.

Safe numeric defaults used during normalization:

- `spl_lower_limit`: `40.0`
- `spl_upper_limit`: `85.0`
- `spl_average_lower_limit`: `40.0`
- `spl_average_upper_limit`: `85.0`
- `loudness_lower_limit`: `0.0`
- `loudness_upper_limit`: `60.0`
- `loudness_average_lower_limit`: `0.0`
- `loudness_average_upper_limit`: `60.0`

## Compatibility / Migration

- Backward compatibility: not required.
- Protected surfaces: current config database schema creation, config load/save dictionaries after this change, config package import/export after this change, factory judgment flow, existing FFT/octave/AI behavior, existing STFT behavior.
- Allowed breakage: existing saved configs and imported payloads missing the new enable flags become unconstrained for SPL/loudness bounds until the operator enables the desired bounds; the home/result summary table gains two new rows; config UI layout changes for SPL and loudness limit controls; summary payload row order expands from five to seven rows.
- Migration strategy: schema migration plus default-off normalization.

Migration details:

- Existing rows get all new enable flags as false.
- Existing rows get average-bound numeric values from defaults: SPL average `40.0` / `85.0`, loudness average `0.0` / `60.0`.
- Existing `spl_lower_limit`, `spl_upper_limit`, `loudness_lower_limit`, and `loudness_upper_limit` values are preserved as stored numeric values, but they do not participate in judgment until their enable flags are true.
- Importing config packages with missing fields fills all missing enable flags as false and fills missing numeric values with defaults.

## Files Likely Affected

- `model/channel_config_table.py`
- `app/service/db_connection_service.py`
- `app/service/config_service.py`
- `app/service/config_package_service.py`
- `app/service/analysis/dsp.py`
- `app/service/analysis/frames.py`
- `app/service/analysis/judgment.py`
- `app/service/analysis/review_payloads.py`
- `ui/modules/config.py`
- `ui/modules/result.py`
- `tests/test_config_service.py`
- `tests/test_config_module.py`
- `tests/test_db_connection_service.py`
- `tests/test_judgment.py`
- `tests/test_review_payloads.py`
- `tests/test_result_module.py`
- `tests/test_frames.py`

## Verification

Focused verification should include:

```powershell
pytest tests/test_config_service.py tests/test_db_connection_service.py tests/test_config_package_service.py -q
pytest tests/test_config_module.py -q
pytest tests/test_judgment.py -q
pytest tests/test_review_payloads.py tests/test_result_module.py tests/test_frames.py -q
```

Focused package-flow verification must explicitly cover export/import round-trips for the new enable flags and numeric value fields.

If the focused set passes, run the broader affected suite:

```powershell
pytest tests/test_test_manager tests/test_factory_mode_controller tests/test_main_window.py -q
```
