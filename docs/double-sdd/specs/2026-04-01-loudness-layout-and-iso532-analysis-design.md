# Loudness Layout And ISO 532 Analysis Design

## Goal

Add an ISO 532 loudness analysis feature to the test page, make it a judged per-channel metric with committed post-analysis visualization, and rearrange the test-page layout to make room for it by removing the system log card, moving video into the former log area, and sending the latest output log line to the bottom status bar.

## Current Context

- The test page is built in `app/main_window.py`.
- The left column currently contains `工单输入`, `控制面板`, and `系统日志`.
- The right grid currently contains `结果摘要`, `视频预览`, `STFT`, `频谱`, `SPL`, and `倍频程`.
- The status bar currently receives runtime state text such as `Idle`, `Recording...`, `Analyzing...`, and error notices through `bus.status`.
- The log UI is currently a dedicated `LogModule` card that appends timestamped lines from `bus.log`.
- Per-channel judged metrics currently include SPL, FFT band limits, and octave band limits. STFT is display-only.
- The analysis pipeline and post-analysis judgment flow already exist in:
  - `app/service/analysis/dsp.py`
  - `app/service/analysis/pipeline.py`
  - `app/service/analysis/judgment.py`
  - `app/service/analysis/review_payloads.py`
- The result summary UI currently renders four fixed rows: `SPL`, `FFT`, `倍频程`, and `AI分析`.
- The current dependency set does not include an ISO 532 loudness library.

## Design

### Recommended Approach

1. Reflow the existing test-page layout with minimal structural change.
   - Remove the dedicated left-side `系统日志` card.
   - Move `视频预览` into the former bottom-left log slot.
   - Place a new `Loudness` chart card into the former `视频预览` grid slot.
   - Keep the existing `结果摘要`, `STFT`, `频谱`, `SPL`, and `倍频程` cards in the same general right-side grid pattern.

2. Replace status-bar state text with the latest output log line.
   - The status bar becomes a single-line log surface that always shows the most recent emitted log message.
   - Existing `bus.status` messages are no longer visible to the operator in the status bar.

3. Add ISO 532-1 loudness via MoSQITo as a committed post-analysis metric.
   - Use `ISO 532-1` (Zwicker) through MoSQITo because it provides a time-varying loudness path suitable for a loudness-over-time chart.
   - Loudness is not live-rendered during recording.
   - Loudness is computed from the committed analysis window used for post-analysis review.

4. Treat loudness like the other judged non-STFT analyses.
   - Add per-channel loudness config fields.
   - Add loudness pass/fail checks into channel judgment and overall pass/fail.
   - Add a `Loudness` summary row and a committed `Loudness` chart card.

### Rejected Alternatives

1. Keep the current layout and combine `SPL` plus loudness into a shared card.
   - Rejected because it weakens visibility and does not match the requested layout.

2. Add loudness as display-only.
   - Rejected because the requested behavior explicitly requires loudness to be a configurable judged item with per-channel limits and pass/fail impact.

3. Use `ISO 532-2` by default.
   - Rejected because MoSQITo already provides a practical `ISO 532-1` time-varying loudness path and this project does not need the added implementation risk of a custom `532-2` route.

4. Keep runtime state text and latest log text together in the status bar.
   - Rejected because the requested behavior is that the latest output log fully replaces the prior status-bar role.

5. Add a separate loudness enable flag.
   - Rejected because loudness should follow the same always-configured pattern as the current judged non-STFT analyses.

## Detailed Design

### Test-Page Layout

The test page will keep its existing left-column plus right-grid structure, but the contents will shift:

- Left column:
  - `工单输入`
  - `控制面板`
  - `视频预览`
- Right grid row 1:
  - `结果摘要`
  - `STFT`
  - `频谱`
- Right grid row 2:
  - `Loudness`
  - `SPL`
  - `倍频程`

This keeps the page visually familiar while adding a dedicated loudness card without introducing tabs, popups, or nested cards.

### Bottom Status Bar Logging

- The status bar becomes the single-line surface for the latest output log message.
- Each `bus.log` emission should update the status bar with the most recent log line.
- The existing `LogModule` card is removed from the main test page.
- The status bar should display the most recent output log line as plain text, not a multi-line history.
- The visible single-line text is derived from each `bus.log` emission by trimming the message and taking its last non-empty line.
- The previous `bus.status -> statusBar().showMessage` behavior is intentionally displaced rather than preserved.
- `bus.status` may continue to exist as an internal signal surface, but it is not mirrored into the visible status bar in this design.
- The loss of direct bottom-bar visibility for current `bus.status` messages such as `Idle`, `Recording...`, `Analyzing...`, `Stopped`, and `Error: ...` is intentional allowed breakage for this task.
- The visible bottom bar has one source of truth only: `bus.log`.
- Existing direct `MainWindow.statusBar().showMessage(...)` calls for startup `Idle` text and current user/role text are removed from the visible status-bar path.
- This feature does not relocate the current user/role text to another surface.
- Before the first log line arrives, the visible status bar may remain empty.
- If startup, login-required, or urgent runtime error states still need bottom-bar visibility after this change, their producers must emit `bus.log` directly.

### Loudness Metric Definition

- Loudness uses `ISO 532-1 (Zwicker)` through MoSQITo.
- The loudness chart is a time-varying committed chart derived from the selected analysis window.
- The chart shape should reuse the existing SPL-style line-chart presentation pattern rather than introducing a new chart framework.
- The judged scalar loudness value for pass/fail is the maximum loudness within the committed analysis window.
- Loudness values are operator-visible in either `sone` or `phon`, depending on the per-channel config.
- `sone` is the canonical internal comparison unit for cross-channel ranking logic.
- For channels configured as `phon`, convert the computed loudness values into `phon` for displayed chart labels, displayed deviations, and pass/fail limit checks for that channel.
- The authoritative unit-conversion helpers live in the DSP layer and are reused everywhere else.
- `sone -> phon` follows the same piecewise relation documented by MoSQITo's `sone2phone` utility:
  - for `sone >= 1`: `phon = 40 + 10 * log2(sone)`
  - for `0 <= sone < 1`: `phon = 40 * (sone + 0.0005) ** 0.35`
- `phon -> sone` uses the algebraic inverse of that same piecewise relation:
  - for `phon >= 40`: `sone = 2 ** ((phon - 40) / 10)`
  - for `0 <= phon < 40`: `sone = (phon / 40) ** (1 / 0.35) - 0.0005`
- Any negative inverse result is clamped to `0 sone`.
- For cross-channel loudness row ranking, convert the actual scalar loudness and both configured bounds into canonical `sone`, then apply the same signed deviation rule already used elsewhere in the result summary:
  - below lower bound: `lower - actual`
  - above upper bound: `actual - upper`
  - inside bounds: negative margin to nearest bound
- The row candidate with the largest canonical signed deviation in `sone` is the selected loudness row.
- The displayed loudness deviation for that chosen channel is then recomputed in that channel's configured display unit so the operator sees `sone` or `phon` consistently with the channel config.
- Both judgment and review-payload construction must call those DSP-layer conversion helpers rather than duplicating conversion logic locally.
- No rounding is applied before loudness pass/fail checks or cross-channel ranking.
- Summary-row deviation values follow the existing review-payload convention of normalizing the final computed display-unit deviation to six decimal places before formatting.

This canonical-internal-unit rule avoids incoherent worst-case ranking when different channels use different display/judgment units.

### Per-Channel Config Additions

Each channel config gains:

- `loudness_unit`: `sone` or `phon`
- `loudness_lower_limit`
- `loudness_upper_limit`

Default values for newly created channel configs:

- unit: `sone`
- lower limit: `0`
- upper limit: `60`

Config validation rules:

- `loudness_unit` must be one of `sone` or `phon`
- `loudness_lower_limit` must be numeric and greater than or equal to `0`
- `loudness_upper_limit` must be numeric and greater than or equal to `0`
- `loudness_lower_limit < loudness_upper_limit`

The config UI should expose the loudness controls alongside the other per-channel analysis settings rather than in a separate global section.

Loudness is always configured for channel rows in this design:

- newly created channel configs receive the default loudness values above
- migrated existing channel rows are backfilled with the same default values
- the loudness summary row is therefore always a constrained row rather than an unconstrained optional row

When a user changes a channel's `loudness_unit` in the config UI:

- the existing numeric lower and upper limit values remain visible as-is
- the app does not auto-convert them
- the app does not reset or clear them
- if the user saves immediately, those unchanged numeric values are persisted under the newly selected unit and are interpreted in that unit during later judgment and display

### Database And Migration

- Add loudness fields to `channel_config_table`.
- Extend the existing DB migration path in `DbConnectionService` so existing databases gain the new loudness columns automatically.
- Extend `ConfigService` load/save/default/validate behavior to include the new loudness fields.

Because backward compatibility is not required for user-visible layout and result-shape surfaces, no compatibility layer is needed for the old 4-row summary or old test-page layout. Schema migration persists and backfills the new loudness fields, and that intentional backfill can change future pass/fail outcomes for existing saved configs.

### Analysis And Judgment Flow

#### Post-Analysis Calculation

- Loudness is computed from the committed analysis window, not from the entire raw recording and not from live runtime chunks.
- The loudness calculation path belongs in the DSP / analysis service layer so UI modules continue to consume plain processed payloads.
- MoSQITo-specific details should stay behind a narrow adapter boundary rather than leaking through the app.
- The loudness adapter consumes the same calibrated mono channel signal used by the existing judged analyses, after `apply_mic_calibration` has converted the raw channel samples into calibrated pressure-domain values.
- The loudness adapter receives:
  - calibrated mono signal for one channel
  - the recorder sample rate
- Any resampling required by the MoSQITo loudness path happens inside the loudness adapter after microphone calibration and before the MoSQITo call.
- The adapter returns, at minimum:
  - loudness trace times
  - loudness trace values in canonical `sone`
  - the maximum loudness scalar in canonical `sone`
- Unit conversion from canonical `sone` into `phon` for channel-configured display and judgment happens only through those DSP-layer loudness helper functions.
- Review-payload construction calls those DSP helpers to convert canonical loudness traces and limits into each channel's configured display unit before storing them in `chart_payload`.
- The UI layer does not perform loudness-unit conversion itself. It renders the converted payload values plus the payload-provided unit metadata.

#### Judgment

For each selected channel:

- compute SPL
- compute loudness
- check FFT band limits
- check octave band limits

For loudness:

- compute the committed loudness-over-time trace across the selected analysis window
- derive the judged scalar as the maximum value from that trace
- compare that scalar against the configured lower and upper loudness limits for the channel
- use that same scalar for deviation calculation and worst-channel ranking
- serialize explicit loudness result fields for later summary construction

The internal serialized judgment contract is extended so loudness summary construction is explicit:

- `ChannelJudgment.loudness_max_sone: float | None`
- `ChannelJudgment.loudness_pass: bool | None`
- `ChannelJudgment.loudness_available: bool`
- `ChannelJudgment.loudness_error_message: str | None`

These serialized fields are authoritative for loudness summary-row pass/fail and loudness no-result decisions after `JudgmentResult` is converted to dictionaries.

Committed loudness chart traces do not need to live inside `JudgmentResult`. `review_payloads.py` may rebuild committed loudness traces from the calibrated analysis-window audio plus config, but it must use the serialized loudness availability/result fields from `JudgmentResult` to decide whether the loudness row is normal or constrained no-result.

The source-of-truth split is explicit:

- serialized loudness judgment fields are authoritative for pass/fail, no-result, and summary-row behavior
- recomputed chart traces are display-only artifacts

If committed loudness chart recomputation fails after serialized judgment succeeded:

- the serialized loudness judgment still governs overall and summary results
- the affected channel omits the `loudness` chart payload and renders an empty loudness chart when selected
- the app emits a log warning for that chart-generation failure

If chart recomputation produces a scalar that disagrees with the serialized loudness judgment result, the serialized judgment result remains authoritative and the discrepancy is logged as a warning instead of changing the summary or overall state.

A channel passes only if all configured non-AI judged analyses pass:

- `SPL`
- `Loudness`
- `FFT`
- `倍频程`

The overall rule combination remains:

- rule-based analyses must pass
- AI must also pass when AI is enabled and returns a valid result

### Result Summary Table

The result summary table expands from four rows to five rows in fixed order:

1. `SPL`
2. `Loudness`
3. `FFT`
4. `倍频程`
5. `AI分析`

The `Loudness` row follows the same worst-case row selection principle used for SPL:

- if any selected channel fails loudness, choose the failing channel with the largest positive deviation
- if all selected channels pass loudness, choose the channel with the smallest remaining margin to failure
- if loudness was expected but no valid loudness result is available, render the row as constrained no-result
- `largest positive deviation` and `smallest remaining margin` are determined from the canonical signed deviation in `sone`, not from mixed display units
- the displayed deviation text for the chosen row is still rendered in that chosen channel's configured unit

Deviation display uses the configured unit for the chosen row’s channel:

- `x sone`
- `y phon`

### Shared Display-Channel Contract

This design also supersedes the older shared display-channel selector contract from `2026-03-26-analysis-results-ui-design.md`.

After this change, the shared analysis display-channel selector applies to five cards instead of four:

- `STFT`
- `Loudness`
- `SPL`
- `FFT`
- `倍频程`

All existing shared selector behaviors remain unchanged unless explicitly noted otherwise:

- toolbar placement remains above the right-side analysis cards
- visibility rules remain tied to the same displayed-config and multi-channel conditions already used by the current toolbar
- selected display channel continues to be remembered per displayed config
- existing fallback behavior remains, but it is governed by the existing shared committed-review channel availability rules for the overall review surface, not by loudness payload availability alone
- a channel that lacks only `loudness` payload but still participates in the committed review surface remains selectable; in that case the loudness card is empty while the other cards continue to use that selected channel

### Review Payload Contract

This design explicitly supersedes the older shared payload contract from `2026-03-26-analysis-results-ui-design.md` for the affected summary-row and committed-chart surfaces.

After this change:

- `summary_payload["rows"]` contains exactly five fixed-order rows:
  1. `SPL`
  2. `Loudness`
  3. `FFT`
  4. `倍频程`
  5. `AI分析`
- `chart_payload[channel_index]` may contain committed payload sections for:
  - `loudness`
  - `spl`
  - `fft`
  - `octave`
- If a channel has no valid committed loudness data, omit the `loudness` key entirely for that channel instead of storing `None` or empty arrays.
- The loudness UI treats a missing or non-dict `loudness` payload as the no-data state for that selected channel.
- Loudness chart conversion and limit rendering use the committed run-time config snapshot for that run, not the current editable config screen state.

The committed loudness chart payload for one channel is already converted into that channel's configured unit and includes enough metadata for direct rendering, at minimum:

- `times_sec`
- `values`
- `unit`
- `lower_limit`
- `upper_limit`

The review-payload layer owns the conversion from canonical loudness results into chart-ready per-channel display units. The UI consumes that payload directly and uses `unit` only for axis labels and display text.

### Committed Loudness Chart

- Add a dedicated committed `Loudness` module/card alongside the existing committed `SPL`, `FFT`, and `倍频程` cards.
- The loudness chart only renders during committed post-analysis review phases, matching the current committed-chart pattern.
- It uses the current selected display channel just like the other committed chart modules.
- It consumes the already converted per-channel loudness chart payload from `review_payloads.py`; it does not convert between `sone` and `phon` itself.
- Its axes should be explicitly labeled:
  - bottom: `Time (s)`
  - left: `Loudness (sone)` or `Loudness (phon)` depending on the selected channel’s configured unit

### Error And No-Result Behavior

If loudness computation fails for a run because MoSQITo is unavailable, raises an error, or produces unusable output:

- the loudness result is treated as unavailable for that run
- the loudness summary row becomes constrained `无结果`
- the overall post-analysis summary becomes `无结果`
- the loudness chart remains empty
- this is a partial no-result review state rather than a full empty-state reset
- successful `SPL`, `FFT`, and `倍频程` rows still render their normal values
- the review error text surfaces the loudness failure message in the same visible error area currently used for constrained no-result summaries

This follows the existing partial-no-result handling pattern already used when a constrained analysis surface becomes unavailable.

If loudness succeeds for some selected channels but fails for others:

- any selected channel with no valid loudness result is treated as a loudness no-result channel
- the overall post-analysis summary still becomes `无结果`
- the loudness summary row is rendered as constrained `无结果`
- successful `SPL`, `FFT`, and `倍频程` rows still render normally
- committed loudness chart payload may still exist for channels whose loudness traces succeeded
- when the operator selects a channel with valid loudness data, the loudness chart renders that committed trace
- when the operator selects a channel with no valid loudness data, the loudness chart is empty for that channel

If AI fails but loudness succeeds:

- `Loudness`, `SPL`, `FFT`, and `倍频程` still render normally
- `AI分析` shows `无结果`
- overall remains `无结果`

### Dependency Management

- Add MoSQITo as a project dependency.
- Keep MoSQITo usage isolated to a DSP helper or adapter boundary.
- Handle sample-rate requirements inside the loudness adapter path rather than spreading resampling concerns into UI modules.

## Testing Strategy

### Config And Persistence

- Add config-service coverage for:
  - loading loudness fields
  - saving loudness fields
  - default loudness field values for newly created channel configs
  - validation failures for invalid unit or invalid bounds
- Add config-module coverage for:
  - loudness widgets populate from loaded config
  - collected save payload includes loudness fields

### Database Migration

- Add migration coverage that existing databases gain the loudness columns.

### DSP / Adapter

- Add focused tests around the loudness adapter boundary:
  - successful `ISO 532-1` loudness computation path
  - `sone` and `phon` output conversion behavior
  - failure path when loudness backend is missing or errors

The tests should validate the app-owned adapter contract rather than binding too tightly to MoSQITo internals.

### Judgment

- Add judgment tests proving loudness affects:
  - per-channel pass/fail
  - overall rule-based pass/fail
  - mixed-unit channels using a coherent worst-case comparison path

### Review Payloads And Result UI

- Add review-payload tests for:
  - five-row order `SPL / Loudness / FFT / 倍频程 / AI分析`
  - loudness deviation formatting in `sone`
  - loudness deviation formatting in `phon`
  - constrained loudness no-result behavior
  - committed loudness chart payload generation

- Add result-module tests for:
  - five-row rendering
  - loudness row content
  - no-result rendering when loudness is unavailable

### Main Window Layout And Status Bar

- Add main-window tests proving:
  - the system log card is removed from the test page
  - the video module is in the former left-bottom slot
  - the loudness module is in the former video slot
  - the status bar shows the latest `bus.log` message
  - the status bar is no longer required to preserve prior runtime state text as its primary visible output

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces: database readability and writeability after adding loudness config columns
- Allowed breakage:
  - old test-page card layout
  - old dedicated `系统日志` card
  - old 4-row result summary shape
  - old status-bar runtime-text behavior
  - existing saved configs gain default loudness limits after migration and can therefore change future pass/fail outcomes
- Migration strategy: direct schema migration for new loudness columns only
