# Normal Distribution Report Design

## Context

Customers want a normal-distribution feature for previous measurement results,
but did not specify which metrics need distribution analysis or how the result
should be presented.

The current application already stores historical runs in `audio_data_table`.
That table includes the WAV path, product model, sample rate, record time,
OK/NG label, barcode, analysis config name, and `config_content` snapshot.
It does not persist SPL, loudness, STFT score, or vibration metrics as separate
queryable columns. The current result dashboard and summary payloads are built
for the active review/run, not for historical aggregate reporting.

Because historical audio files and config snapshots are available, the report
feature can compute historical metrics from the saved WAV and the config
snapshot, then cache the derived metric rows for later report generation.

The historical table does not store the microphone calibration snapshot used at
the original measurement time. Historical recomputation therefore uses the
current `device_table.mic_cal` data exposed through
`HardwareSelectService.load_io_devices()`, matching the application's existing
manual import/reanalysis behavior. Cache validity must include a calibration
hash so reports are recomputed when current calibration changes.

## Goals

1. Add a historical statistics report that can be viewed inside the software.
2. Allow users to filter report input by time range, product model, labels, and
   optional barcode text, reusing the data-management filtering semantics.
3. Support an unfiltered report over all historical audio records.
4. Compute and display normal-distribution-oriented statistics for existing
   products and acoustic metrics.
5. Export the same report data to an HTML report and a CSV detail file.
6. Add a database cache table for derived historical metric rows so repeated
   report generation does not reanalyze unchanged WAV files.
7. Keep existing measurement, review, data-management, and package-export
   behavior unchanged except for the new reporting entry points.

## Non-Goals

- Do not change the live analysis algorithms.
- Do not change OK/NG judgment behavior.
- Do not change the meaning of existing labels, manual labels, or AI labels.
- Do not require customers to configure which metrics are available before the
  report can run.
- Do not introduce a PDF dependency in the first implementation.
- Do not add browser-engine dependencies for in-software viewing.
- Do not persist raw chart arrays, full STFT matrices, or duplicated audio data
  in the cache table.
- Do not modify historical WAV files or existing `audio_data_table` rows while
  generating a report.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces: existing `audio_data_table` records, existing audio data
  ZIP export, existing runtime result dashboard, existing configuration loading,
  existing WAV files, existing chart CSV/PNG export, existing login/tab
  permissions, and current OK/NG label semantics.
- Allowed breakage: a prepared runtime database must include the new cache
  table after migration/preparation. No existing rows or columns should be
  rewritten.
- Migration strategy: add a new derived-cache table. Existing historical records
  are backfilled lazily when users generate reports. If the cache row is absent,
  stale, or failed under an older algorithm version, report generation may
  recompute it from the saved WAV and `config_content`.

## User Experience

### Entry Point

Add a `统计报告` action to the `数据管理` page toolbar.

The action opens an in-software report window/dialog. The report window should
use application-native PyQt widgets and pyqtgraph plots, not embedded HTML.

The data-management table remains the source of historical records, but the
report window owns its own filter state so users can generate a report without
selecting rows in the data table.

### Filters

The report window supports:

- product model: `ALL` plus values from `AudioDataService.list_product_models()`
- optional time range: start/end datetime, disabled means all dates
- labels: OK, NG, and unlabeled using the existing data-management semantics
- optional barcode substring

Default filters:

- product model: `ALL`
- time range: disabled
- labels: OK and NG selected; unlabeled not selected by default
- barcode: empty

Users can still include unlabeled records manually.

### Report View

The report window displays:

- filter summary
- total historical records matched by the filter
- number of records with valid cached/computed metrics
- number of skipped records and failed computations
- OK/NG/unlabeled counts
- per-metric statistics table
- per-metric histogram with an overlaid normal fitted curve
- list/table of skipped or failed records with reason
- list/table of outlier records

The view should remain useful when only some metrics are available. A failed
metric on one record must not prevent other records or metrics from appearing.

### Export

The report window has an export action that writes:

1. an HTML report with summary tables and chart images/data embedded or written
   next to the HTML file
2. a CSV detail file containing one row per record/channel/metric value used by
   the report

The HTML report should open directly in a browser without a server.

The CSV file should use UTF-8 with BOM so Chinese headers open correctly in
common Windows spreadsheet tools.

## Metric Scope

Report metrics are derived per historical record and selected channel, excluding
the configured vibration-only channel from acoustic metrics.

For this report, "selected channel" means the valid non-negative channel indexes
stored in the historical config snapshot's `selected_channels`. The first
implementation does not add a channel filter to the report UI.

For each historical record:

- acoustic metrics are computed for every valid selected channel except the
  configured vibration channel
- vibration metrics are computed only for the configured vibration channel
- if `selected_channels` is empty or invalid, metric computation fails for that
  record with a record-level cache error instead of guessing channels from the
  WAV file

### Acoustic Metrics

Include these metrics when available:

- `spl_peak_db`: `ChannelJudgment.spl_max_db` from `JudgmentEngine`; if the
  trace max is unavailable but `spl_db` is finite, use `spl_db`
- `spl_average_db`: `ChannelJudgment.spl_average_db`, computed by
  `dsp.compute_spl_leq()` through `JudgmentEngine`
- `loudness_peak_sone`: `ChannelJudgment.loudness_max_sone`
- `loudness_average_sone`: `ChannelJudgment.loudness_average_sone`
- `stft_score`: `chart_payload[channel]["stft_score"]["summary_score"]` from
  `build_chart_payload()` / `_build_stft_score_payload()`, which delegates to
  `stft_score.compute_stft_score_payload()`

Loudness is cached in sone as the canonical numeric unit. The UI may display
sone and, where helpful, also show the channel's configured unit in labels.

### Vibration Metrics

If the config snapshot has a valid `vibration_view_channel_index`, include
vibration metrics for that channel when the payload builder can compute them:

- `vibration_rms`: `build_vibration_payload()["overall_rms"]`
- `vibration_peak`: `build_vibration_payload()["overall_peak"]`
- `vibration_peak_factor`: `build_vibration_payload()["overall_crest_factor"]`
- `vibration_main_frequency_hz`: `build_vibration_payload()["dominant_frequency_hz"]`

If no vibration channel exists, omit vibration sections entirely.

### Metric Labels

Use Chinese user-visible labels:

- 声压级峰值
- 平均声压级
- 响度峰值
- 平均响度
- 频谱最低得分
- 振动 RMS
- 振动峰值
- 振动峰值因子
- 振动主频

## Statistical Semantics

For each metric group, compute statistics over finite numeric values only.

Metric grouping dimensions:

- product model, using `audio_data_table.product_model`; empty model displays
  as `未填写型号`
- metric key
- channel index or canonical channel label

The report may provide an `全部型号` aggregate in addition to per-model groups.

Per group, compute:

- sample count `n`
- mean
- sample standard deviation
- min
- max
- P5
- median/P50
- P95
- normal-fit curve using the group mean and standard deviation

Use sample standard deviation (`ddof=1`) when `n >= 2`; otherwise the standard
deviation is unavailable.

Percentiles must use NumPy's default linear percentile method:

```python
np.percentile(values, [5, 50, 95], method="linear")
```

If the installed NumPy only supports the older keyword, use the equivalent
`interpolation="linear"` behavior.

### Normal Curve Display

For `n >= 5` and finite positive standard deviation, show:

- histogram of actual values
- normal fitted curve using the group's mean and standard deviation

For `n < 5` or zero/unavailable standard deviation, show the values and basic
statistics, but display a clear sample-insufficient state instead of a normal
curve.

### Outliers

Flag outliers per metric group using both:

- outside mean +/- 3 standard deviations when standard deviation is available
- outside P5/P95 percentile band when `n >= 20`

Outliers are report annotations only. They must not change stored OK/NG labels.

## Cache Table

Add a new derived-cache table named `historical_metric_cache_table`.

The table stores one metric value per audio record/channel/metric key.

Required fields:

- `cache_id`: auto primary key
- `audio_data_id`: text, indexed, references the historical audio row by id
- `file_path`: text snapshot used during computation
- `file_size`: integer snapshot
- `file_mtime_ns`: integer snapshot
- `record_date`: text snapshot
- `product_model`: text snapshot
- `barcode`: text nullable snapshot
- `label`: text nullable snapshot
- `label_source`: text nullable snapshot
- `analyse_config`: text nullable snapshot
- `config_hash`: text hash of the effective config snapshot
- `calibration_hash`: text hash of the current mic calibration used for
  computation
- `algorithm_version`: text
- `channel_index`: integer, using `-1` for record-level rows
- `channel_label`: text
- `metric_key`: text
- `metric_label`: text
- `metric_value`: real nullable; null for failed/skipped rows
- `metric_unit`: text
- `status`: text, one of `ok`, `skipped`, `failed`
- `error_message`: text nullable
- `computed_at`: text timestamp

Recommended unique key:

- `(audio_data_id, config_hash, algorithm_version, channel_index, metric_key)`

Recommended indexes:

- `audio_data_id`
- `(product_model, record_date)`
- `(metric_key, status)`

Record-level failures use a sentinel cache row:

- `channel_index`: `-1`
- `channel_label`: `记录`
- `metric_key`: `__record__`
- `metric_label`: `记录状态`
- `metric_unit`: empty string
- `metric_value`: null
- `status`: `skipped` or `failed`
- `error_message`: the user-visible reason

Because `channel_index` is never null, the recommended unique key prevents
duplicate record-level failure rows for the same source/config/version.

`algorithm_version` is owned by the historical report service as a module-level
constant, for example `HISTORICAL_REPORT_ALGORITHM_VERSION = "2026-06-18.1"`.
It must be bumped whenever any historical metric value could change for the
same WAV/config pair, including changes to calibration application, analysis
window handling, SPL/loudness/STFT/vibration extraction, or metric-key
definitions. Pure UI/export formatting changes do not require a bump.

### Cache Validity

A cache row is valid when all of these match the current source record:

- `audio_data_id`
- normalized `file_path`
- `file_size`
- `file_mtime_ns`
- `config_hash`
- `calibration_hash`
- `algorithm_version`

If any value differs, recompute the record's metric cache rows.

Cache refresh is record-scoped. When a record needs recomputation, the service
must run a transaction that deletes all existing cache rows for that
`audio_data_id`, then inserts the fresh rows for the current file/config/current
calibration/algorithm version. Stale rows are not versioned and must not be
kept alongside current rows. This avoids unique-key collisions when file
metadata or calibration changes without changing `config_hash` or
`algorithm_version`.

Mutable historical metadata (`record_date`, `product_model`, `barcode`,
`label`, `label_source`, and `analyse_config`) does not affect metric-cache
validity because it does not change computed metric values. Report filtering,
display, and export must join against the current `audio_data_table` row for
these fields. Cache snapshots of those fields are audit/debug information only.

If the WAV file is missing, unreadable, unsupported, or the config snapshot is
missing/invalid, write one or more `failed`/`skipped` cache rows so the report
can explain why the record was not included. Failed cache rows should also be
invalidated by algorithm version changes.

For failure rows where source details are unavailable, use deterministic
sentinel values so validity can still be evaluated:

- missing/empty `file_path`: `file_path=""`, `file_size=-1`,
  `file_mtime_ns=-1`
- file path exists in the row but the file cannot be stat/read:
  normalized `file_path`, `file_size=-1`, `file_mtime_ns=-1`
- missing `config_content`: `config_hash="__missing_config__"`
- invalid `config_content`: `config_hash="__invalid_config__:<sha256 of raw text>"`
- missing/unreadable calibration: `calibration_hash` is the hash of `{}` and
  computation proceeds with no-op calibration unless the metric itself fails

If a missing file later appears, or invalid config text changes, the sentinel
metadata no longer matches and the record is eligible for recomputation.

## Computation Flow

1. Build the record query using the same filter semantics as
   `AudioDataService._apply_filters()`.
2. For each matched record, inspect the cache validity.
3. If valid rows exist for the current algorithm version, use them.
4. If cache rows are missing or stale, load the WAV and parse
   `audio_data_table.config_content`.
5. Load current microphone calibration from
   `HardwareSelectService.load_io_devices()["mic_cal"]`; if unavailable, use
   `{}` and include that value in `calibration_hash`.
6. Reuse existing analysis code paths where practical:
   - WAV loading and sample normalization from imported-audio analysis
   - `JudgmentEngine.rejudge()` over the configured analysis window for SPL and
     loudness values
   - `build_chart_payload()` for STFT score and vibration payload values
7. Store derived metric rows in the cache table inside a transaction.
8. Aggregate all valid `ok` metric rows for the report view/export.

Historical computation uses the config snapshot's
`analysis_window_offset_ms` and `analysis_duration`. If the saved WAV does not
cover that window, write a record-level failure row.

Channel mapping for normal historical recordings must account for the current
runtime save path: `TestSession.capture_recorder_buffer()` persists a projected
buffer containing only selected channels in selected-channel order. A config
with `selected_channels=[3]` is therefore stored as WAV column `0`.

For report recomputation, build:

```python
channel_column_map = {
    selected_channel: column_index
    for column_index, selected_channel in enumerate(selected_channels)
}
```

The first implementation supports only this projected runtime WAV format. The
WAV channel count must equal `len(selected_channels)`. If it is smaller or
larger, write a record-level failure row. Larger full-hardware WAV layouts must
not be guessed because the same file has no persisted marker that distinguishes
full-hardware channel order from projected selected-channel order.

The first implementation may compute records sequentially in a background
thread with progress and cancellation. The UI must not freeze during bulk
backfill.

## Error Handling

Per-record failures are expected and should be visible, not fatal.

Examples:

- missing WAV path
- deleted WAV file
- invalid WAV data
- missing `config_content`
- invalid config JSON
- config references unavailable channel
- analysis window exceeds available audio
- metric algorithm failure

The report should still generate when at least one metric value is available.
If no valid metric values are available, show an empty report state with the
failure table and allow CSV export of failures.

## Permissions

The report entry point belongs to the existing `数据管理` page. Any role that
can open `数据管理` can open the statistics report and export it. Operator role
does not gain this page through the report feature.

## Presentation Guidance

The in-app report is an operational tool, not a marketing page.

Use a dense but readable layout:

- compact filter toolbar at the top
- summary counters below the toolbar
- metric table and chart area in the main body
- metric selection list or tabs for switching charts
- failure/outlier tables at the bottom or in secondary tabs

Avoid nested cards. Use existing `Card` only for the top-level data-management
container or repeated report panels if consistent with local UI patterns.

## Export Details

### HTML

The HTML export should include:

- title: `测量结果正态分布统计报告`
- generated time
- filter summary
- data coverage summary
- per-metric statistics table
- histogram/normal-fit chart for each metric group
- failure and outlier tables

Use static HTML/CSS/JavaScript or pre-rendered chart images/data generated by
the application. The exported HTML must not require network resources.

### CSV

The detail CSV should include:

- `audio_data_id`
- `record_date`
- `product_model`
- `barcode`
- `label`
- `label_source`
- `analyse_config`
- `channel_label`
- `metric_key`
- `metric_label`
- `metric_value`
- `metric_unit`
- `status`
- `error_message`

The CSV always includes every cache row used for the report, including `ok`,
`skipped`, and `failed` rows. This keeps exported reports auditable and avoids a
separate failure-only export mode.

## Testing

Add service-level tests for:

- cache table model/creation assumptions
- filter query reuse for product model, time, labels, and barcode
- cache validity based on file metadata, config hash, and algorithm version
- cache invalidation/recomputation when current `mic_cal` changes and therefore
  `calibration_hash` changes
- lazy backfill from a small generated WAV
- missing WAV produces failed/skipped cache rows without aborting the report
- invalid config snapshot is reported per record
- aggregation ignores non-finite and failed metric values
- statistics use sample standard deviation and correct percentiles
- normal curve is suppressed for insufficient sample counts
- CSV detail export uses expected Chinese/English headers and UTF-8 BOM
- HTML export includes summary, metric tables, charts, and failure rows

Add UI-level tests for:

- `数据管理` shows the `统计报告` action
- opening the report window does not require selecting table rows
- report filters default to all models, no time range, OK/NG labels
- generating a report starts background computation and updates progress
- cancellation leaves the UI usable
- completed report displays summary counters, metrics, and failure rows
- export action calls the report export service with current report data

Run focused tests first:

- `pytest tests/test_audio_data_service.py tests/test_data_management_module.py`
- new report service tests
- new report UI tests

Broaden only after focused tests pass, and report unrelated existing failures
separately.
