# Golden Sample FFT And Octave Design

## Goal

Add per-channel golden-sample support to `FFT` and `1/3 倍频程` so operators can import a reference WAV in `配置`, compare against it visually, subtract it into a delta view, and optionally make it part of pass/fail judgment.

The feature must support two independent golden-sample capabilities for both `FFT` and `octave`:

1. subtract the golden sample and judge the delta curve
2. draw the golden sample curve on the chart for comparison

This first version supports imported audio only. Historical-run selection is explicitly out of scope because run-log persistence does not exist yet.

## Current Context

- Per-channel configuration is edited in `ui/modules/config.py`.
- Per-channel configuration is persisted through:
  - `app/service/config_service.py`
  - `model/channel_config_table.py`
  - `app/service/db_connection_service.py`
- `FFT` and octave are already judged per-channel items.
- The committed post-analysis charts are rendered from `FactoryReviewPayload.chart_payload` in:
  - `ui/modules/spectrum.py`
  - `ui/modules/octave.py`
- The chart widgets currently render only a primary series plus per-band limit markers in:
  - `ui/widgets/analysis_charts.py`
- Post-analysis summary rows are built in `app/service/analysis/review_payloads.py`.
- Runtime pass/fail is computed in `app/service/analysis/judgment.py`.
- The committed chart payload is rebuilt from the selected analysis window in `app/service/analysis/review_payloads.py`.
- The app already saves WAV files for recorded runs, but there is no persisted run-history selection surface for v1 golden-sample binding.

## Scope

In scope:

- imported-WAV golden-sample binding inside the per-channel config UI
- one shared golden sample per test channel, reused by that channel's `FFT` and `octave`
- per-analysis golden modes for `FFT` and `octave`
- committed chart overlay and delta rendering
- pass/fail judgment using the reused existing band-limit tables
- save validation for golden bindings and impossible golden-mode combinations
- persistence for the new per-channel golden fields
- runtime and committed-review error handling for missing or unreadable golden assets

Out of scope:

- historical-run golden-sample selection
- non-WAV golden sources
- automatic threshold conversion from absolute limits into golden-relative limits
- compatibility behavior for older configs or older schemas
- STFT golden-sample behavior
- loudness or SPL golden-sample behavior

## Approved Decisions

1. Golden-sample source is WAV only.
2. The first version supports imported audio only.
3. Golden binding is per test channel, not per config.
4. One shared golden source is used by both `FFT` and `octave` for that channel.
5. `FFT` and `octave` each keep independent golden behavior settings on top of the shared source.
6. The imported golden source must match the test sample rate.
7. The imported WAV must be at least as long as the config `analysis_duration`.
8. If the imported WAV is longer than `analysis_duration`, the user must choose a golden window with exact length `analysis_duration`.
9. The golden import UI lives in `配置`, not in the FFT or octave cards.
10. The first version needs an actual waveform/time-selection UI, not only raw numeric input.
11. If `analysis_duration` changes after a golden sample is bound, the binding becomes invalid and save is blocked until the user clears or rebuilds it.
12. `FFT` golden judgment modes are:
    - `Off`
    - `Absolute`
    - `Delta`
13. `octave` golden judgment modes are:
    - `Off`
    - `Absolute`
    - `Delta`
14. `Show golden curve` is separate from judgment mode.
15. Golden overlay is allowed even when judgment mode is `Off`.
16. Golden overlay is valid only in absolute-y-space charts, not in delta charts.
17. Existing FFT and octave band-limit tables are reused. The app does not create separate golden-threshold tables.
18. No backward-compatibility layer is required.
19. Current FFT behavior is normatively full-window FFT; golden FFT must use that same full-window path.

## Recommended Approach

Use a per-channel managed golden asset instead of keeping a loose external file reference.

The import flow will:

1. let the operator choose a WAV
2. preview the selected source channel in an import dialog
3. force the operator to choose a fixed-width window equal to `analysis_duration`
4. copy that exact mono clip into app-managed storage
5. persist the resulting managed asset path plus binding metadata on the channel config

This approach is recommended because it solves the hard alignment problems up front:

- the imported golden asset is already mono
- the imported golden asset already has exact duration equal to `analysis_duration`
- the imported golden asset already matches the config sample rate

As a result:

- FFT measured and golden series naturally share the same frequency axis
- octave measured and golden series naturally share the same center-frequency axis
- the implementation does not need interpolation, nearest-bin snapping, or dual-source runtime trimming in v1

## Detailed Design

### Per-Channel Config Model

Each channel config gains a shared golden-source block:

- `golden_sample_path: str | None`
  - absolute path to the managed committed mono golden WAV
  - must live under the app-owned golden-sample root
  - must be unique to that one channel binding
- `golden_sample_label: str | None`
  - user-facing label, usually the original imported filename
- `golden_sample_source_channel: int | None`
  - zero-based source channel chosen from the original imported WAV
- `golden_sample_start_sample: int | None`
  - zero-based sample-aligned window start inside the original imported WAV
- `golden_sample_num_samples: int | None`
  - canonical imported clip width in samples
- `golden_sample_start_ms: int | None`
  - derived display metadata computed from `golden_sample_start_sample`
- `golden_sample_duration_ms: int | None`
  - derived display metadata computed from `golden_sample_num_samples`
- `golden_sample_sample_rate: float | None`
  - sample rate of the imported WAV; must equal the current configured test sample rate

Each channel config also gains FFT-specific golden controls:

- `fft_golden_mode: "off" | "absolute" | "delta"`
- `fft_show_golden_overlay: bool`

Each channel config also gains octave-specific golden controls:

- `oct_golden_mode: "off" | "absolute" | "delta"`
- `oct_show_golden_overlay: bool`

Staging note:

- staged import metadata is editor-only state and is not persisted directly
- the persisted `golden_sample_path` always points to the committed managed asset path only
- canonical duration matching is sample-count based, not raw floating-second equality
- millisecond fields are display metadata only and are normalized from canonical sample-aligned values on import and on successful save

Recommended defaults for new channel rows:

- `golden_sample_*`: `None`
- `fft_golden_mode`: `off`
- `fft_show_golden_overlay`: `False`
- `oct_golden_mode`: `off`
- `oct_show_golden_overlay`: `False`

### Config UI

Add a new shared `Golden Sample` section to each channel page in `ui/modules/config.py`.

That section owns:

- current golden binding summary
- `Import WAV...`
- `Replace WAV...`
- `Clear`
- inline validity state

The summary must show at minimum:

- bound filename or label
- stored sample rate
- stored source channel
- stored window start
- stored window duration
- current validity status

`FFT` and `1/3 倍频程` keep their own existing sections, but each gains:

- a `golden mode` selector with values `Off / Absolute / Delta`
- a `show golden curve` checkbox

UI behavior:

- the golden section is shared by the channel, not duplicated inside both analysis sections
- `show golden curve` remains available when mode is `Off`
- when mode is `Delta`, the corresponding overlay checkbox is disabled and forced off in the live UI
- config validation also rejects persisted `Delta + overlay=true` combinations as a guardrail
- clearing or invalidating the shared golden binding does not silently rewrite FFT/octave golden settings
- instead, the UI shows the binding as invalid and save is blocked until the user:
  - reimports a valid golden sample, or
  - clears the invalid golden sample entirely
- turning off the dependent golden settings alone is not enough to bypass a stale bound asset; an invalid existing binding itself is a save blocker until it is rebuilt or removed

### Golden Import Dialog

The import flow begins from the new per-channel `Golden Sample` section.

The operator chooses a WAV file, then the app opens an import dialog with:

- source-channel selector
- waveform preview for the selected source channel
- fixed-width selection region whose width is exactly the current `analysis_duration`
- numeric start-time control in integer milliseconds, synchronized with the selection region
- read-only derived end-time display
- import summary showing:
  - file sample rate
  - channel count
  - total duration
  - required golden duration

Validation in the dialog:

- if the config sample rate is missing or invalid, import is disabled because the golden WAV cannot be validated
- if the current `analysis_duration` is missing, zero, negative, or otherwise invalid, import is disabled because the fixed-width golden window cannot be defined
- if the WAV sample rate does not equal the current test sample rate, reject the import
- if the WAV duration is shorter than `analysis_duration`, reject the import
- if the WAV duration is exactly equal to `analysis_duration`, the selection region is fixed at `0`
- if the WAV duration is longer than `analysis_duration`, the operator must choose a valid start position

On confirm:

- compute `required_golden_samples = round(sample_rate * analysis_duration)` once and use that as the canonical clip width
- convert the selected UI start-time value in milliseconds into a sample-aligned start index
- extract the selected source channel only
- clip exactly `required_golden_samples` samples
- stage the result as a managed mono WAV under app-owned storage
- return staged golden-binding metadata to the channel editor

The dialog returns editor-only staged metadata that includes at minimum:

- staged asset path
- label
- source channel
- start sample
- normalized start time in integer milliseconds
- normalized duration in integer milliseconds
- canonical sample count
- sample rate

### Managed Asset Storage

Imported golden samples are stored as app-managed assets under a dedicated directory such as:

- `data/golden_samples/...`

The stored asset is:

- mono
- exact duration equal to the current `analysis_duration`
- same sample rate as the current test sample rate
- located under the managed golden root only
- single-owned by one channel binding only; committed asset paths are never shared across channels or configs

The runtime analysis path reads this managed mono clip directly. It does not reopen the original user-selected multichannel file.

Asset lifecycle rules:

- replacing a golden sample writes a new staged asset instead of immediately deleting the previous committed asset
- clearing a golden sample removes the binding but defers old committed-asset deletion until after config save succeeds
- staged assets are promoted to committed managed asset paths during the save attempt before the DB write, and they become authoritative only if the DB write also succeeds
- if the user cancels config edits, changes selection, or config save fails, newly staged assets are discarded and the previously committed asset remains authoritative
- config-save code must avoid orphaning replaced assets
- the managed asset path is the authoritative runtime source

Authoritative save ordering:

1. The config editor keeps staged metadata in memory only; it does not write staged paths into the DB payload.
2. On save, the save flow is the sole authority that resolves a unique future committed asset path under the managed golden root for each staged import and injects only that future committed path plus the non-path metadata into the config payload.
3. The save flow copies or renames each staged asset into its future committed path before the DB write, while leaving every previously committed asset untouched and recording every newly promoted file path for rollback.
4. If promotion for any staged import fails, all newly promoted files created earlier in the same save attempt are deleted, no old committed assets are touched, and the DB write does not run.
5. The DB write then persists only committed asset paths.
6. If the DB write succeeds, old replaced assets and old cleared assets are deleted, and the staged temporary files are cleared.
7. If the DB write fails after promotion, every newly promoted asset created in that save attempt is deleted, all old committed assets remain authoritative, and the editor surfaces save failure without changing the persisted binding.
8. If post-commit cleanup of old assets or staged temp files fails after the DB write has already succeeded, the DB result remains authoritative; the app logs a warning and leaves deferred cleanup to a later maintenance pass instead of attempting rollback.

### Save Validation

Config validation is extended with these rules:

1. A bound golden sample must be self-consistent.
   - for an already committed binding, `golden_sample_path` must exist, be readable, and live under the managed golden root
   - `golden_sample_sample_rate` must be numeric and equal to the current configured test sample rate
   - `golden_sample_start_sample` must be a non-negative integer
   - `golden_sample_num_samples` must be an integer equal to `required_golden_samples = round(sample_rate * analysis_duration)`
   - the managed asset's actual sample count must equal `golden_sample_num_samples`
   - `golden_sample_start_ms` must equal `round(golden_sample_start_sample * 1000 / sample_rate)`
   - `golden_sample_duration_ms` must equal `round(golden_sample_num_samples * 1000 / sample_rate)`
   - `golden_sample_source_channel` must be a non-negative integer

   For a newly staged import that has not been saved yet:

   - the staged asset path must exist and be readable in editor state
   - the future committed path does not need to exist yet
   - save validation must validate the staged asset plus metadata before promotion, then validate the committed path after promotion and before DB commit

2. Any stale binding is invalid.
   - changing `analysis_duration` invalidates previously imported assets
   - changing the input-device sample rate invalidates previously imported assets
   - an invalid bound asset blocks save even if all golden modes are set to `Off`

3. Golden-dependent analysis settings require a valid binding.
   - if `fft_golden_mode != off`, a valid shared golden binding is required
   - if `oct_golden_mode != off`, a valid shared golden binding is required
   - if `fft_show_golden_overlay` is `True`, a valid shared golden binding is required
   - if `oct_show_golden_overlay` is `True`, a valid shared golden binding is required

4. Impossible combinations are rejected.
   - `fft_golden_mode == delta` with `fft_show_golden_overlay == True` is invalid
   - `oct_golden_mode == delta` with `oct_show_golden_overlay == True` is invalid

5. Empty band tables remain allowed.
   - `Absolute` or `Delta` golden mode may be used even if the corresponding band-limit table is empty
   - in that case the chart behavior changes, but judgment remains unconstrained for that item

### Runtime Golden Reference Computation

For each selected channel with a valid golden binding, the runtime path derives reference curves from the managed mono golden asset using the current channel analysis settings.

The golden asset is treated as the same raw signal domain as the measured test audio. Therefore, when runtime analysis derives FFT or octave references from the imported golden clip, it must apply the same microphone-calibration treatment used for the measured signal on that channel before computing golden FFT or octave levels.

The fixed part of the golden definition is:

- imported managed mono clip
- selected source channel from the original WAV, stored as metadata only
- selected start time in the original WAV, stored as metadata only

The derived part is recomputed from the current settings:

- FFT reference curve
- octave reference curve

This means:

- changing FFT or octave settings changes the derived golden reference on the next run/reanalysis
- changing `analysis_duration` or sample rate invalidates the binding entirely instead of silently stretching or resampling it

Sample-rate source of truth:

- config-time import and save validation key off the configured `input_device_samplerate`
- runtime judgment and committed chart building key off the actual recorder sample rate passed into the analysis layer for that run
- a bound golden sample is runtime-valid only when:
  - `golden_sample_sample_rate == configured input_device_samplerate`, and
  - `golden_sample_sample_rate == actual runtime sample_rate`
- if the actual runtime sample rate differs from the stored golden sample rate, golden-enabled FFT/octave judgment becomes constrained `无结果`

Performance requirement:

- golden reference loading and per-analysis computation must happen once per selected channel per committed post-analysis pass where practical
- the implementation must not repeatedly reread the same golden asset in avoidable inner loops

### FFT Semantics

Golden FFT must use the same full-window FFT path as the normal FFT chart and FFT judgment.

For one channel:

- measured FFT is computed from the selected analysis window
- golden FFT is computed from the managed mono golden clip
- because both signals have the same sample rate and the same exact duration, their FFT axes match exactly in v1

Configured FFT band frequencies still map to discrete FFT bins. The binding rule remains the existing nearest-bin rule:

- for each configured FFT band frequency, choose `argmin(abs(freq_axis - target_freq))`
- use that same matched FFT bin index for:
  - measured level selection
  - golden level selection
  - effective limit-band serialization
  - committed chart marker payload

Because measured and golden FFT axes are identical in v1, this single nearest-bin rule is sufficient and no measured-vs-golden interpolation step is needed.

FFT golden modes:

- `Off`
  - plotted main series: measured absolute FFT
  - pass/fail: existing absolute band-limit behavior
  - overlay: optional

- `Absolute`
  - plotted main series: measured absolute FFT
  - pass/fail: band limits are interpreted around the golden absolute level at each configured band
  - effective rule per configured band:
    - `golden_level + lower <= measured_level <= golden_level + upper`
  - overlay: optional

- `Delta`
  - plotted main series: `measured_level - golden_level`
  - pass/fail: existing band table is interpreted directly in delta space
  - effective rule per configured band:
    - `lower <= delta_level <= upper`
  - overlay: not allowed

### Octave Semantics

Golden octave uses the same weighted octave computation as the normal octave chart and judgment.

For one channel:

- measured octave is computed from the selected analysis window
- golden octave is computed from the managed mono golden clip using the current octave weighting
- because both signals have the same sample rate and the same exact duration, the octave center-frequency axis matches exactly in v1

Octave golden modes:

- `Off`
  - plotted main series: measured absolute octave levels
  - pass/fail: existing absolute band-limit behavior
  - overlay: optional

- `Absolute`
  - plotted main series: measured absolute octave levels
  - pass/fail: band limits are interpreted around the golden absolute octave level at each configured band
  - effective rule per configured band:
    - `golden_level + lower <= measured_level <= golden_level + upper`
  - overlay: optional

- `Delta`
  - plotted main series: `measured_level - golden_level`
  - pass/fail: existing band table is interpreted directly in delta space
  - effective rule per configured band:
    - `lower <= delta_level <= upper`
  - overlay: not allowed

### Judgment Contract

The existing summary-row logic should keep working without a special golden-summary fork.

To bridge runtime golden-reference failures into the existing summary path, `ChannelJudgment` must gain explicit per-item availability fields for the affected analyses:

- `fft_available: bool`
- `fft_error_message: str | None`
- `octave_available: bool`
- `octave_error_message: str | None`

These fields are authoritative for summary-row `无结果` behavior when golden-enabled FFT or octave judgment cannot be completed.

To achieve that, FFT and octave golden-enabled band checks must serialize `BandResult` values in the same y-space that was actually judged:

- `Off`
  - `level = measured absolute level`
  - `lower = configured lower`
  - `upper = configured upper`

- `Absolute`
  - `level = measured absolute level`
  - `lower = golden absolute level + configured lower`
  - `upper = golden absolute level + configured upper`

- `Delta`
  - `level = measured absolute level - golden absolute level`
  - `lower = configured lower`
  - `upper = configured upper`

This preserves the current summary-row contract:

- the FFT row still chooses the worst FFT band candidate across selected channels
- the octave row still chooses the worst octave band candidate across selected channels
- deviation display remains in `dB`

If a golden-enabled judged analysis cannot load or compute its golden reference at runtime:

- the corresponding item becomes constrained `无结果`
- the review summary becomes partial `无结果`
- the overall review result becomes `无结果`
- `fft_available` or `octave_available` is set to `False`
- the corresponding error message field is populated
- the corresponding `fft_band_results` or `oct_band_results` list is treated as unavailable rather than as an empty pass case

If the item is only using overlay with `mode = Off` and golden loading fails:

- pass/fail remains based on the existing non-golden rules
- the chart omits the overlay
- the app logs a warning

### Committed Chart Payload Contract

`FactoryReviewPayload.chart_payload[channel_index]["fft"]` is extended with:

- `frequencies_hz`
- `levels_db`
  - always the main plotted series
  - absolute in `Off` and `Absolute`
  - delta in `Delta`
- `limit_bands`
  - always in the same y-space as `levels_db`
- `golden_mode`
  - `off | absolute | delta`
- `golden_overlay_levels_db`
  - optional absolute golden comparison series
  - present only when the chart is in absolute-y-space and overlay is enabled
- `show_zero_reference`
  - `True` only in `Delta`

`FactoryReviewPayload.chart_payload[channel_index]["octave"]` is extended with:

- `center_freqs_hz`
- `levels_db`
  - always the main plotted series
  - absolute in `Off` and `Absolute`
  - delta in `Delta`
- `limit_bands`
  - always in the same y-space as `levels_db`
- `golden_mode`
  - `off | absolute | delta`
- `golden_overlay_levels_db`
  - optional absolute golden comparison series
  - present only when the chart is in absolute-y-space and overlay is enabled
- `show_zero_reference`
  - `True` only in `Delta`

The chart-payload builder should prefer the serialized band results from `JudgmentResult` when constructing golden-aware limit markers so the chart markers exactly match the judged thresholds and outcomes.

Failure serialization rules:

- if golden-enabled FFT judgment fails because the golden reference cannot be loaded or computed, omit the `fft` item entirely from that channel's committed `chart_payload`
- if golden-enabled octave judgment fails because the golden reference cannot be loaded or computed, omit the `octave` item entirely from that channel's committed `chart_payload`
- do not store `None`, partial error dicts, or placeholder series under those keys
- the summary payload carries the constrained `无结果` state; the selected chart module shows an empty chart for the affected channel
- if the failure happens only while attempting an optional overlay with `mode = Off`, keep the normal non-golden `fft` or `octave` payload and omit only the overlay metadata
- if serialized judgment already succeeded but later committed-chart recomputation for FFT or octave fails, serialized judgment remains authoritative for summary and overall review state; the chart payload omits the affected item or overlay and the app logs a warning

### Chart Rendering

`FftLineChartWidget` and `OctaveBarChartWidget` are extended to render:

- one main plotted series
- optional absolute golden overlay series
- the existing per-band limit markers, but in the current plotted y-space
- an optional horizontal zero-reference line for delta mode

Chart rules:

- `Off`
  - main plot remains the measured absolute curve or bars
  - overlay may appear if enabled

- `Absolute`
  - main plot remains the measured absolute curve or bars
  - overlay may appear if enabled

- `Delta`
  - main plot becomes the delta curve or delta bars
  - zero-reference line is shown
  - overlay is hidden

The existing limit-marker style should be preserved.

This design does not invent a continuous interpolated limit envelope because the app still configures limits as discrete FFT or octave bands. In delta mode, the existing per-band limit markers simply move into delta y-space while the zero-reference line provides the global visual anchor.

### Review-Phase Modules

`ui/modules/spectrum.py` and `ui/modules/octave.py` continue to render committed data only.

They must:

- consume the richer payload shape
- pass overlay and zero-reference metadata into the chart widgets
- stay empty when the selected channel lacks the needed committed payload

The shared review-channel selection rules remain unchanged:

- each card still renders the currently selected committed review channel when data exists
- fallback behavior still uses the existing committed-review channel-selection rules only when the selected channel itself is absent from the committed channel map
- if the selected committed channel exists in `chart_payload` but its `fft` or `octave` item key is omitted because golden-aware rendering failed for that item, the corresponding module must keep the selected channel and render an empty chart instead of falling back to another channel

### Error Handling

Config-time invalid cases:

- stale golden asset after analysis-duration change
- stale golden asset after sample-rate change
- missing or unreadable golden asset path
- impossible `delta + overlay` combination

These are surfaced inline in the config UI and block save.

Runtime invalid cases:

- golden asset deleted after config save
- golden asset unreadable
- golden asset corrupted
- golden asset fails to compute FFT or octave reference data

Handling:

- for golden-enabled judgment modes, the affected FFT or octave item becomes constrained `无结果`
- for overlay-only usage with `mode = Off`, the overlay is omitted and a warning is logged

## Testing Strategy

### Config And Persistence

Add coverage for:

- loading and saving all new per-channel golden fields
- defaults for new channel rows
- rejecting stale `golden_sample_num_samples`
- normalizing derived `golden_sample_start_ms` / `golden_sample_duration_ms` metadata from canonical sample-aligned values
- rejecting stale `golden_sample_sample_rate`
- rejecting missing or unreadable `golden_sample_path`
- rejecting `delta + overlay`
- allowing empty band tables with golden modes

### Config UI

Add coverage for:

- populating the shared `Golden Sample` section from loaded config
- collecting the new golden fields into save payloads
- disabling overlay when `Delta` mode is selected
- surfacing invalid-binding state after analysis-duration changes

### Golden Import Flow

Add focused tests for:

- reading WAV metadata
- rejecting sample-rate mismatch
- rejecting files shorter than `analysis_duration`
- channel selection in multichannel imports
- fixed-width window selection
- managed mono clip creation

### Judgment

Add coverage proving:

- `Off` mode preserves current absolute behavior
- `Absolute` mode uses `golden + configured bounds`
- `Delta` mode uses `measured - golden`
- serialized `BandResult` values are emitted in the judged y-space
- missing golden data produces constrained `无结果` for golden-enabled judgment modes

### Review Payloads

Add coverage for:

- FFT payload absolute mode with overlay
- FFT payload delta mode with zero reference
- octave payload absolute mode with overlay
- octave payload delta mode with zero reference
- marker payloads remaining aligned with judged results

### Chart Widgets And Modules

Add coverage for:

- optional FFT overlay rendering
- optional octave overlay rendering
- FFT zero-reference rendering in delta mode
- octave zero-reference rendering in delta mode
- octave delta bars remaining renderable with negative values
- committed spectrum and octave modules consuming the richer payload shape

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - per-channel configuration ownership model
  - committed-review-only rendering model for FFT and octave cards
  - existing non-golden behavior when all golden settings are `Off`
- Allowed breakage:
  - `channel_config_table` schema
  - config payload shape
  - FFT and octave committed chart payload shape
  - stricter save validation for sample rate and analysis duration changes
- Migration strategy: none
