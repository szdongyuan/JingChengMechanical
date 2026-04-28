# Homepage Live SPL And Loudness Metrics Design

## Purpose

Operators need the result summary card on the home/test page to expose the most important acoustic scalar values without waiting for or reading the detailed plots:

- During recording, show the current sound pressure level and loudness for the active display channel.
- After analysis, show the average sound pressure level and average loudness for the same display channel.
- Keep the existing FAIL/OK result badge and detailed summary table behavior intact.

The new values are display-only indicators. They do not change judgment, persistence, chart rendering, or factory control decisions.

## Approved Decisions

1. Add two large metric controls below the existing FAIL/OK badge in the `结果摘要` card.
2. The controls are visually similar in weight to the existing result badge, but are read-only display indicators.
3. Before a running/displayed configuration is known, both metrics show `--`.
4. The application remembers the user's most recent display-channel selection only for the current process lifetime.
5. When a configuration becomes active/displayed, the metric channel is resolved as:
   - Use the remembered channel if it exists in that configuration's `selected_channels`.
   - Otherwise use the first selected channel for that configuration.
   - If no selected channel is available, show `--`.
6. The metrics follow the same display channel as the analysis charts once the channel toolbar is available.
7. User channel changes update the process-lifetime remembered channel and refresh the metrics.
8. During recording, the SPL metric shows the latest real-time SPL from the existing `spl_updated` signal for the resolved channel.
9. During recording, the loudness metric first attempts to compute real-time loudness with the existing ISO 532 / MoSQITo backend and uses throttling to avoid running it for every audio chunk.
10. After analysis, the SPL metric shows `spl_average_db` from the selected channel's serialized judgment result.
11. After analysis, the loudness metric shows `loudness_average_sone` from the selected channel's serialized judgment result, converted to the channel's configured loudness unit for display.
12. If a metric cannot be computed, is unavailable, or does not match the current run/config/channel, it shows `--` instead of stale data.

## Scope

In scope:

- Add the two read-only metric indicators to `ui/modules/result.py`.
- Extend home-page channel-selection state in `app/main_window.py` so the last selected channel is remembered for this application run and used when resolving a newly displayed configuration.
- Feed `ResultModule` with a selected-display-channel provider and refresh hook, matching the existing analysis chart modules.
- Use existing real-time SPL frames for recording-time SPL display.
- Add a throttled real-time loudness computation path for recording-time loudness display.
- Use existing `judgment_payload.channel_results` fields for post-analysis average SPL and average loudness display.
- Add focused tests for metric rendering, channel memory/fallback, stale-frame rejection, and post-analysis average display.
- Add focused tests proving reanalysis clears prior live/review metrics to `--` until the replacement review payload arrives, then renders only the replacement averages.

Out of scope:

- Persisting the user's last selected channel across application restarts.
- Changing summary payload row structure or judgment decisions.
- Adding new database fields.
- Changing SPL, loudness, FFT, octave, STFT, or AI judgment algorithms.
- Adding new limit checks.
- Changing the detailed SPL/loudness chart behavior.

## UX Contract

The `结果摘要` card layout becomes:

1. Top result line with `总结果：` and the existing FAIL/OK/no-result badge.
2. A two-column metric row below the result badge:
   - Sound pressure metric.
   - Loudness metric.
3. The existing error message area and detailed summary table stay below those metrics.

Display text:

- Idle / no configuration / no metric value:
  - Label: `声压级`
  - Value: `--`
  - Label: `响度`
  - Value: `--`
- Recording:
  - Label: `声压级`
  - Value: latest selected-channel SPL, formatted as `<value> dB`
  - Label: `响度`
  - Value: latest selected-channel loudness, formatted as `<value> sone` or `<value> phon`
- Reviewing/stopped after successful analysis:
  - Label: `平均声压级`
  - Value: selected-channel `spl_average_db`, formatted as `<value> dB`
  - Label: `平均响度`
  - Value: selected-channel `loudness_average_sone` converted to the selected channel's configured `loudness_unit`, formatted as `<value> sone` or `<value> phon`

Recommended numeric formatting:

- SPL: one decimal place when finite, e.g. `62.4 dB`.
- Sone loudness: two decimal places for values below 10 and one decimal place for larger values, e.g. `3.25 sone`, `12.4 sone`.
- Phon loudness: one decimal place, e.g. `48.2 phon`.
- Non-finite, missing, mismatched, or stale values display `--`.

The metric controls should not look clickable. They are large read-only indicators, not commands.

## Channel Resolution

`MainWindow` already tracks display channels per displayed config for analysis charts. This feature adds a process-lifetime remembered channel independent of any one config.

Resolution rules:

1. If no displayed/active config ID exists, no channel is resolved.
2. Load the displayed config's `selected_channels`.
3. If the remembered channel exists in `selected_channels`, use it.
4. Else use the first selected channel.
5. Store the resolved channel in the existing per-config display-channel map so chart modules and the metric module agree.

The process-lifetime remembered channel is the only cross-run preference. An older per-config remembered channel must not override the fallback rule when the process-lifetime remembered channel is invalid for the current configuration.

When the user changes channel from the analysis channel toolbar:

- Update the existing per-config display-channel map.
- Update the process-lifetime remembered channel.
- Refresh all analysis display modules and the result metrics.

This preserves the user's latest channel preference across sequential runs while avoiding invalid channel selections for configurations that do not contain that channel.

## Runtime Data Flow

Live run identity:

- `ResultModule` must track the currently displayed live run ID.
- It listens to `factory_runtime_start_accepted(FactoryRuntimeStartPayload)` and stores `payload.run_id` when `payload.config_id` matches the displayed or active config.
- It listens to `factory_runtime_analyzing_entered(FactoryRuntimeConfigPayload)` and keeps the same run ID when the config matches; this preserves the final live values through the analyzing phase until the review payload arrives.
- It clears the tracked live run ID on `factory_display_reset`, on a new matching `factory_runtime_start_accepted`, and when the displayed config changes to a different config.
- Live SPL/loudness frames are accepted only when their `run_id` equals this tracked live run ID. If no tracked live run ID exists, live frames are ignored.

SPL during recording:

- `TestSession` already emits `spl_updated(config_id, run_id, channel_idx, SplFrame)`.
- `ResultModule` listens for this signal.
- A frame updates the SPL metric only when:
  - `config_id` equals the current displayed config ID.
  - `run_id` matches the tracked live run ID.
  - `channel_idx` equals the currently resolved display channel.
  - the app is in a recording/live phase.
  - `SplFrame.spl_db` is finite.

Loudness during recording:

- Add a new `LoudnessFrame` dataclass in `app/service/analysis/frames.py`:
  - `time: float`
  - `loudness_sone: float`
- Add a new event bus signal in `app/services_container.py`:
  - `loudness_updated = pyqtSignal(int, str, int, object)`
  - Payload order matches the other live analysis signals: `config_id`, `run_id`, `channel_idx`, `LoudnessFrame`.
- Compute live loudness inside `AnalysisPipeline.process_chunk()` after microphone calibration, not in `ResultModule`.
- `AnalysisPipeline` owns the per-channel throttle state because it already owns calibrated per-channel streaming data.
- Use constants in `app/service/analysis/pipeline.py`:
  - `_LIVE_LOUDNESS_THROTTLE_SEC = 1.0`
  - `_LIVE_LOUDNESS_WINDOW_SEC = 1.0`
- Maintain a rolling calibrated loudness buffer per pipeline, capped to `_LIVE_LOUDNESS_WINDOW_SEC`.
- On each chunk, emit at most one loudness frame if `elapsed_time - last_live_loudness_emit_time >= _LIVE_LOUDNESS_THROTTLE_SEC`.
- Compute live loudness with `dsp.compute_loudness_trace(rolling_window, sample_rate)`.
- Use the latest finite value from the returned sone trace as `LoudnessFrame.loudness_sone`.
- If no finite value exists or the backend raises, return no loudness frame for that chunk.
- `TestSession._process_chunk()` emits `bus.loudness_updated(config_id, run_id, channel_idx, loudness_frame)` when `AnalysisPipeline` returns a loudness frame.
- The UI filters the signal to the current displayed config/run/channel exactly like SPL.
- Live loudness is computed for each selected channel's pipeline, but the UI displays only the resolved display channel. This keeps pipeline ownership simple and preserves immediate response when the user switches channels.
- Live loudness failures must not stop recording, analysis, chart rendering, or judgment. To avoid log spam, backend failures may be silent in the pipeline; tests should assert non-fatal behavior rather than log contents.
- The live backend returns canonical sones. `ResultModule` converts `LoudnessFrame.loudness_sone` to the selected channel's configured `loudness_unit` using the same conversion helpers used by review payloads.

Post-analysis averages:

- `ResultModule` reads `FactoryReviewPayload.judgment_payload.channel_results`.
- For the resolved channel:
  - `spl_average_db` drives average SPL.
  - `loudness_average_sone` drives average loudness.
- The configured channel `loudness_unit` controls display conversion for average loudness.
- If the resolved channel has no serialized result or the relevant value is missing/non-finite, show `--`.

## State And Staleness Rules

- On factory display reset, clear live metric values and show `--`.
- On config change, run start, or selected-channel change, reject old live frames from the previous config/run/channel.
- During `running`, prefer live values.
- During `analyzing`, keep the latest matching live value until an analysis result arrives, unless the displayed config/run changes.
- During `reanalyzing`, show `--` for both metrics until the replacement review payload arrives. Reanalysis has no live recording frames, and stale values from the prior run or prior review must not be displayed as if they belonged to the pending reanalysis result.
- During `reviewing` or `stopped`, prefer post-analysis average values from the current review payload.
- During `idle`, `armed`, `not_ready`, or other non-display phases with no current review payload, show `--`.

## Compatibility / Migration

- Backward compatibility: required.
- Protected surfaces: existing summary payload format, existing judgment payload format, database schema, config save/load dictionaries, factory mode state transitions, analysis chart rendering, existing tests for result summary rows.
- Allowed breakage: none intended.
- Migration strategy: none. The feature uses process-memory UI state and existing serialized judgment fields.

## Error Handling

- Missing config, missing channel config, missing selected channels, or invalid channel indexes result in `--`.
- Loudness backend import/runtime errors during live display are non-fatal and do not affect final judgment.
- Non-finite numeric values are treated as unavailable.
- A stale frame from a previous config, previous run, or non-selected channel is ignored.

## Files Likely Affected

- `app/services_container.py`
- `app/main_window.py`
- `app/service/analysis/test_session.py`
- `app/service/analysis/pipeline.py`
- `app/service/analysis/frames.py`
- `ui/modules/result.py`
- `tests/test_result_module.py`
- `tests/test_main_window.py`
- `tests/test_service_container.py`
- `tests/test_test_session.py`
- `tests/test_pipeline.py` or a focused analysis live-metric test file
- `tests/test_test_manager/test_review_payload_emission.py` if a new runtime signal is added at manager/session level

## Verification

Focused verification should include:

```powershell
pytest tests/test_result_module.py -q
pytest tests/test_main_window.py -q
pytest tests/test_service_container.py tests/test_test_session.py -q
pytest tests/test_pipeline.py -q
```

If a new runtime signal or session-level loudness path is added, include the affected manager/session tests:

```powershell
pytest tests/test_test_manager -q
```

Broader confidence verification:

```powershell
pytest tests/test_review_payloads.py tests/test_judgment.py tests/test_analysis_chart_modules -q
```
