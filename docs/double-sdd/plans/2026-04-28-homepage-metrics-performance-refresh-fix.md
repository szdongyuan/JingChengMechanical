# Homepage Metrics Performance And Refresh Fix Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix homepage SPL/loudness metric performance and refresh behavior without changing judgment, charts, or persisted config.

**Architecture:** Route the currently displayed channel from `MainWindow` to `TestManager`/`TestSession`, so only that channel runs the expensive live loudness backend. Keep live metric caches keyed by config/run/channel in `ResultModule`, apply UI-only SPL throttling, and continue using serialized judgment averages for post-analysis metrics.

**Tech Stack:** Python, PyQt6 signals/widgets, NumPy, pytest

---

## Spec

- `docs/double-sdd/specs/2026-04-28-homepage-metrics-performance-refresh-fix-design.md`

## File Structure

- Modify `app/services_container.py`
  - Add `EventBus.factory_display_channel_changed`.
- Modify `app/main_window.py`
  - Emit the display-channel signal after validated channel resolution/selection.
- Modify `app/service/analysis/test_manager.py`
  - Track display channel per config and apply it to active sessions.
- Modify `app/service/analysis/test_session.py`
  - Store live loudness channel and pass per-pipeline `compute_loudness`.
- Modify `app/service/analysis/pipeline.py`
  - Add `compute_loudness` argument and reduce loudness throttle while preserving the rolling window.
- Modify `ui/modules/result.py`
  - Keep per-channel live caches on channel switch and throttle SPL badge repaint only.
- Modify tests:
  - `tests/test_service_container.py`
  - `tests/test_pipeline.py`
  - `tests/test_test_session.py`
  - `tests/test_main_window.py`
  - `tests/test_result_module.py`
  - `tests/test_test_manager/test_rejudge.py` or `tests/test_test_manager/test_review_payload_emission.py` only if existing assertions need the new signal stub.

## Task 1: Route Display Channel To Live Loudness Computation

**Files:**
- Modify: `app/services_container.py`
- Modify: `app/main_window.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `app/service/analysis/test_session.py`
- Modify: `app/service/analysis/pipeline.py`
- Test: `tests/test_service_container.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_test_session.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing routing and gating tests**

Add/extend tests to prove:

- `EventBus` exposes `factory_display_channel_changed` with `(int, int)` argument types.
- `MainWindow` emits `factory_display_channel_changed(config_id, channel_index)` after initial display-channel resolution and after toolbar channel changes.
- `TestManager` stores display-channel changes and applies them to a running `TestSession`.
- `TestManager.start()` applies a stored display-channel announcement to a newly created session after `_setup_pipelines()` and before chunks can be processed.
- `TestSession._setup_pipelines()` defaults live loudness channel to the first selected channel.
- `TestSession._process_chunk()` passes `compute_loudness=True` only for the current live loudness channel.
- `AnalysisPipeline.process_chunk(..., compute_loudness=False)` does not call `dsp.compute_loudness_trace()` but still maintains the rolling loudness buffer.
- Live loudness throttle is `0.25s`; the rolling window remains `0.5s`.

- [ ] **Step 2: Run focused failing tests**

Run:

```powershell
pytest tests/test_service_container.py tests/test_pipeline.py tests/test_test_session.py tests/test_main_window.py -q
```

Expected: FAIL before implementation.

- [ ] **Step 3: Implement display-channel routing**

Implement:

- `EventBus.factory_display_channel_changed = pyqtSignal(int, int)`.
- In `MainWindow`, emit the signal only after a channel is validated and stored for the current displayed config.
- In `TestManager.__init__`, connect to the signal when present and maintain `_live_loudness_channel_by_config_id`.
- In `TestManager.start()`, after `session._setup_pipelines()`, apply the stored channel for that config if present.
- Validate the stored channel against `config["selected_channels"]`; if invalid or absent, keep the session's first-selected fallback.
- In `TestSession`, add:
  - `_live_loudness_channel_index: int | None`
  - `set_live_loudness_channel_index(channel_index: int | None)`.
  - default first selected channel in `_setup_pipelines()`.
  - per-pipeline `compute_loudness=(ch_idx == self._live_loudness_channel_index)`.
- In `AnalysisPipeline.process_chunk()`, add keyword-only or optional `compute_loudness: bool = True`, skip `_maybe_compute_live_loudness()` when false, and set `_LIVE_LOUDNESS_THROTTLE_SEC` to `0.25` and `_LIVE_LOUDNESS_WINDOW_SEC` to `0.5`.

- [ ] **Step 4: Re-run focused tests**

Run:

```powershell
pytest tests/test_service_container.py tests/test_pipeline.py tests/test_test_session.py tests/test_main_window.py -q
```

Expected: PASS.

## Task 2: Fix ResultModule Metric Refresh And Average Replacement

**Files:**
- Modify: `ui/modules/result.py`
- Test: `tests/test_result_module.py`
- Test: `tests/test_judgment.py` only for an analysis-window regression if needed.

- [ ] **Step 1: Write failing ResultModule regressions**

Add/extend tests to prove:

- Switching channels during a live run immediately renders cached SPL/loudness values for the newly selected channel when they exist.
- Switching channels no longer clears all current-run metric caches.
- Matching SPL frames update the cache but repaint the SPL badge no more often than `_SPL_METRIC_UI_THROTTLE_SEC = 0.25`.
- The first valid SPL frame for a config/run/channel repaints immediately.
- If a valid SPL frame has missing or non-finite `frame.time`, throttle comparison uses monotonic wall time and still respects `_SPL_METRIC_UI_THROTTLE_SEC`.
- A selected-channel refresh repaints immediately from the cache, regardless of SPL throttle state.
- A matching non-finite SPL frame clears that key and its throttle timestamp, repaints `--`, and allows the next valid frame to repaint immediately.
- Loudness frames repaint immediately when they arrive.
- Reanalysis start with empty replacement payload shows `--` and suppresses old average metrics while `run_phase == "reanalyzing"`.
- Reanalysis replacement review payloads update average metrics from the new `judgment_payload`.
- Switching channels during `reviewing` or `stopped` immediately re-renders average metrics from the current replacement review payload for the newly selected channel.
- A review payload whose `displayed_config_id` does not match the current displayed config must not render average metrics.
- Review averages render only in `reviewing` or `stopped`, not during `running`, `analyzing`, `reanalyzing`, `armed`, or `idle`.
- A production `JudgmentEngine` payload with a non-zero analysis window produces badge average SPL from that window, proving the badge reuses serialized judgment averages.
- Missing selected-channel result or missing/non-finite average fields display `--` per metric without hiding the other valid metric.

- [ ] **Step 2: Run focused failing tests**

Run:

```powershell
pytest tests/test_result_module.py -q
```

Expected: FAIL before implementation.

- [ ] **Step 3: Implement refresh fixes**

Implement:

- Remove current-run cache clearing from `refresh_selected_display_channel()`.
- Implement scoped live cache invalidation exactly:
  - new matching run removes cache entries and SPL repaint timestamps for the displayed config whose run id differs from the new run id;
  - displayed-config change removes cache entries and SPL repaint timestamps for the previous displayed config and clears current live run id;
  - factory reset clears all live metric caches, all SPL repaint timestamps, and current live run id;
  - reanalysis start clears displayed metric labels to `--` and suppresses old review averages without deleting unrelated cached live values.
- Add `_SPL_METRIC_UI_THROTTLE_SEC = 0.25`.
- On accepted matching SPL frame:
  - update the keyed SPL cache;
  - derive throttle time from finite `frame.time`, otherwise use monotonic wall time;
  - repaint only if enough throttle time has elapsed since the previous SPL metric repaint for that key, or if the current label is `--`;
  - clear the key and its repaint timestamp immediately on non-finite values.
- On accepted matching loudness frame, update cache and repaint immediately.
- On selected-channel refresh, bypass SPL throttle and repaint from current live cache or current review payload according to phase.
- Before rendering post-analysis averages, require:
  - `self._review_payload.displayed_config_id == self._displayed_config_id()`;
  - phase is `reviewing` or `stopped`;
  - `judgment_payload` is a dict.
- Keep post-analysis average rendering as a read from selected channel `judgment_payload.channel_results`; do not add any new final-average computation in `ResultModule`.

- [ ] **Step 4: Re-run focused tests**

Run:

```powershell
pytest tests/test_result_module.py -q
```

Expected: PASS.

## Task 3: Integrated Verification

**Files:**
- No intended source changes unless verification finds a real issue.

- [ ] **Step 1: Run feature-focused verification**

Run:

```powershell
pytest tests/test_service_container.py tests/test_pipeline.py tests/test_test_session.py tests/test_main_window.py tests/test_result_module.py -q
```

Expected: PASS.

- [ ] **Step 2: Run affected analysis/review verification**

Run:

```powershell
pytest tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py tests/test_review_payloads.py tests/test_judgment.py -q
```

Expected: PASS or report unrelated pre-existing failures with evidence.

- [ ] **Step 3: Leave branch ready for orchestration**

Do not merge or push from the worker. List changed files and verification results in the final response.
