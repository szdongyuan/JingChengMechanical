# Homepage Metrics Performance And Refresh Fix Design

## Purpose

The homepage result-summary SPL/loudness metric follow-up must address the runtime issues found after the first implementation:

- Live loudness currently spends too much CPU because every selected channel computes ISO 532 loudness even though the UI can display only one channel.
- Live loudness refresh feels too slow, while live SPL refreshes too often in the result-summary metric badges.
- Switching the display channel should immediately show the last known metric values for that same config/run/channel when they exist, instead of clearing all values and waiting for the next frame.
- Manual reanalysis must replace the metric values with the newly serialized average results.
- Final average SPL and average loudness shown in the badges must come from the judgment result for the active analysis window, not from full-recording recomputation.

The existing summary rows, judgment behavior, charts, and persisted config remain unchanged.

## Approved Decisions

1. Live loudness is computed only for the currently resolved display channel for the active config/run.
2. `MainWindow` announces the resolved display channel through the event bus whenever it is initially resolved or changed by the user.
3. `TestManager`/`TestSession` use that announced channel to decide which pipeline is allowed to run live loudness. Other channel pipelines keep their rolling loudness buffers but skip the expensive backend call.
4. If no display-channel announcement has been received for a running config, the session falls back to the first selected channel.
5. Live loudness refresh target is faster than before: throttle at `0.25s` with a `0.5s` rolling buffer.
6. SPL frames continue to be emitted at the existing cadence for charts and other consumers, but `ResultModule` throttles only the metric-badge UI update to about `4 Hz`.
7. `ResultModule` keeps per-config/run/channel live metric caches. Channel switching must not clear the whole current-run cache; it should display cached values for the newly selected channel when available.
8. New run, displayed-config change, factory reset, and reanalysis start still clear inappropriate stale values.
9. Post-analysis metric badges only read `spl_average_db` and `loudness_average_sone` from the selected channel in `FactoryReviewPayload.judgment_payload.channel_results`.
10. Manual reanalysis completion must update the badges from the replacement judgment payload.

## Scope

In scope:

- Add a display-channel event bus signal.
- Route display-channel changes from `MainWindow` to `TestManager` and active `TestSession`.
- Add an optional live-loudness compute flag to `AnalysisPipeline.process_chunk()`.
- Reduce live loudness throttle while preserving the rolling window constant.
- Add metric-badge SPL UI throttling in `ResultModule`.
- Preserve per-channel live metric caches across channel switches within the same config/run.
- Add focused regression tests for CPU gating, refresh behavior, channel switching, and analysis-window averages.

Out of scope:

- Changing judgment algorithms.
- Changing summary table rows or chart payload schema.
- Persisting the display-channel preference across process restarts.
- Throttling or changing the existing `spl_updated` signal cadence.
- Adding a new live loudness algorithm.

## Runtime Contract

`EventBus` gains:

- `factory_display_channel_changed = pyqtSignal(int, int)`
- Arguments are `config_id`, `channel_index`.

`MainWindow` emits the signal after the display channel has been validated against the displayed config's selected channels:

- during initial toolbar synchronization for a displayed config;
- after a user toolbar channel selection;
- after fallback channel resolution for a new displayed config.

`TestManager` listens to this signal and stores the latest live metric display channel per config. When a matching session is active, it updates that session immediately.

`TestSession`:

- Defaults its live loudness channel to the first selected channel when pipelines are created.
- If `TestManager` has already received a display-channel announcement for the config before the session is created, it must apply that stored channel after `_setup_pipelines()` and before recorder chunks can be processed. The first-selected-channel fallback only applies when there is no stored announcement for the config or the stored channel is not in `selected_channels`.
- Exposes `set_live_loudness_channel_index(channel_index)`.
- In `_process_chunk()`, passes `compute_loudness=True` only to the pipeline whose channel matches the current live loudness channel.
- Emits `loudness_updated` only when that pipeline returns a `LoudnessFrame`.

`AnalysisPipeline.process_chunk()`:

- Accepts `compute_loudness: bool = True` for backward compatibility.
- Always maintains the rolling loudness buffer after calibration.
- Calls `dsp.compute_loudness_trace()` only when `compute_loudness` is true and the throttle allows it.
- Uses `_LIVE_LOUDNESS_THROTTLE_SEC = 0.25` and `_LIVE_LOUDNESS_WINDOW_SEC = 0.5`.

`ResultModule`:

- Stores live values by `(config_id, run_id, channel_index)`.
- Does not delete all current-run cached values on display-channel changes.
- Re-renders from the cache for the newly selected channel.
- Applies UI-only SPL badge throttling with `_SPL_METRIC_UI_THROTTLE_SEC = 0.25`.
- The first valid SPL frame for a key must repaint immediately.
- A valid SPL frame repaints when `frame.time - last_spl_repaint_time >= 0.25`. If `frame.time` is missing or non-finite, the handler may use monotonic wall time for the throttle comparison.
- Skipped SPL frames still update the cache.
- A selected-channel refresh repaints from the latest cached value immediately, regardless of the SPL throttle timestamp.
- A matching non-finite SPL frame clears that key's SPL cache, clears its last repaint timestamp, and repaints immediately to `--` if the key is currently displayed. The next valid SPL frame for that key must repaint immediately.
- Does not throttle loudness badge updates because the backend path already throttles loudness frames.
- Renders post-analysis averages from `judgment_payload` whenever the current review payload belongs to the displayed config and the phase is `reviewing` or `stopped`.

Live metric cache invalidation:

- New matching `factory_runtime_start_accepted` for `(config_id, new_run_id)` sets the current live run to `new_run_id` and removes all live metric cache entries for the displayed config whose run id differs from `new_run_id`.
- Displayed-config change clears the current live run id and removes all live metric cache entries for the previous displayed config.
- Factory display reset clears the current live run id, all live metric caches, and all SPL repaint timestamps.
- Reanalysis start (`run_phase == "reanalyzing"` with a review payload whose `summary_payload` and `judgment_payload` are absent) clears displayed metric values to `--` and suppresses old review averages until a replacement review payload arrives.
- Channel switching during `running` or `analyzing` preserves live metric caches for the current config/run and immediately renders cached values for the newly selected channel, or `--` if that channel has no cached value.
- Channel switching during `reviewing` or `stopped` immediately re-renders from the current replacement review payload for the newly selected channel.

## Analysis-Window Average Contract

The badge must not compute final averages itself. It must display exactly the selected channel fields already serialized by judgment:

- `spl_average_db`
- `loudness_average_sone`

Because `JudgmentEngine.judge()` and `JudgmentEngine.rejudge()` run on their selected analysis window before serialization, the metric badges inherit the analysis-window semantics by consuming the serialized fields. Regression tests should use a non-zero analysis window or reanalysis replacement payload to guard this behavior.

If the selected/display channel is absent from `judgment_payload.channel_results`, or either serialized average field is missing, null, or non-finite, the corresponding badge value displays `--`. SPL and loudness availability are evaluated independently; a missing loudness average must not hide a valid SPL average for the same channel, and vice versa.

## Compatibility / Migration

- Backward compatibility: required.
- Protected surfaces: existing summary payload format, existing judgment payload format, database schema, config save/load dictionaries, existing chart behavior, existing live SPL signal consumers.
- Allowed breakage: none intended.
- Migration strategy: none.

## Verification

Focused verification:

```powershell
pytest tests/test_service_container.py tests/test_pipeline.py tests/test_test_session.py -q
pytest tests/test_result_module.py -q
pytest tests/test_main_window.py -q
pytest tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py -q
```

Broader confidence:

```powershell
pytest tests/test_review_payloads.py tests/test_judgment.py -q
```
