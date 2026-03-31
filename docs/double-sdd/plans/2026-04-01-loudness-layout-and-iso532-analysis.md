# Loudness Layout And ISO 532 Analysis Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add committed ISO 532 loudness analysis with per-channel config and judgment, replace the test-page log card with a log-driven status bar, move video into the old log area, and place a loudness chart in the old video slot.

**Architecture:** Keep MoSQITo integration isolated behind DSP-layer loudness helpers that produce canonical `sone`, then extend config, judgment, review-payload, and UI layers to consume those results. Reuse the existing committed-chart and shared display-channel patterns rather than inventing a new rendering path.

**Tech Stack:** Python, PyQt6, pyqtgraph, NumPy, SciPy, Peewee, pytest, MoSQITo

---

## File Structure

- Modify: `requirements.txt`
  - Add the MoSQITo dependency used by the DSP loudness helpers.
- Modify: `app/service/analysis/dsp.py`
  - Add loudness adapter and `sone`/`phon` conversion helpers.
- Modify: `app/service/analysis/frames.py`
  - Extend judgment dataclasses with loudness result fields.
- Modify: `app/service/analysis/judgment.py`
  - Compute loudness max, pass/fail, no-result fields, and serialize them through `ChannelJudgment`.
- Modify: `app/service/analysis/review_payloads.py`
  - Add loudness summary-row logic and committed loudness chart payload generation.
- Modify: `app/service/analysis/test_manager.py`
  - Keep serialized loudness judgment authoritative while letting chart payload recomputation stay display-only.
- Modify: `app/service/config_service.py`
  - Add loudness defaults, save/load behavior, and validation.
- Modify: `app/service/db_connection_service.py`
  - Add migration for loudness columns.
- Modify: `model/channel_config_table.py`
  - Persist loudness unit and bounds.
- Modify: `ui/modules/config.py`
  - Add per-channel loudness controls.
- Create: `ui/modules/loudness.py`
  - Render committed loudness chart data using the same shared display-channel contract as SPL/FFT/octave.
- Modify: `ui/modules/result.py`
  - Expand to a five-row summary and render loudness row content.
- Modify: `app/main_window.py`
  - Rearrange cards, wire status bar to latest log line, and include the loudness card in shared display-channel behavior.
- Modify: `tests/test_dsp.py`
  - Cover conversion helpers and loudness adapter behavior.
- Modify: `tests/test_frames.py`
  - Cover the extended loudness fields on serialized analysis dataclasses.
- Modify: `tests/test_judgment.py`
  - Cover loudness pass/fail and no-result judgment behavior.
- Modify: `tests/test_review_payloads.py`
  - Cover the five-row payload and loudness chart payload contract.
- Modify: `tests/test_result_module.py`
  - Cover loudness row rendering and partial-no-result behavior.
- Modify: `tests/test_main_window.py`
  - Cover the layout swap, status-bar log behavior, and shared channel selection including loudness.
- Modify: `tests/test_test_manager/test_rejudge.py`
  - Update review-payload assertions from the old 4-row contract to the new 5-row contract.
- Modify: `tests/test_test_manager/test_review_payload_emission.py`
  - Update emitted summary payload expectations for the new loudness row.
- Modify: `tests/test_test_manager/_support.py`
  - Update shared summary payload helpers used by test-manager review tests.
- Modify: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`
  - Update controller-side review payload fixtures and assertions for the 5-row summary.
- Modify: `tests/test_config_service.py`
  - Cover loudness persistence and validation.
- Modify: `tests/test_config_module.py`
  - Cover loudness controls in the config UI.
- Modify: `tests/test_db_connection_service.py`
  - Cover migration of the new loudness columns.
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - Cover the new committed loudness module and empty/fallback behavior.

### Task 1: Add Loudness DSP Helpers And Judgment Contracts

**Files:**
- Modify: `requirements.txt`
- Modify: `app/service/analysis/dsp.py`
- Modify: `app/service/analysis/frames.py`
- Modify: `app/service/analysis/judgment.py`
- Modify: `tests/test_dsp.py`
- Modify: `tests/test_frames.py`
- Modify: `tests/test_judgment.py`

- [ ] **Step 1: Write failing DSP and judgment tests for loudness**

Add focused tests that lock the new behavior before implementation:

```python
def test_sone_to_phon_matches_piecewise_contract():
    assert sone_to_phon(1.0) == pytest.approx(40.0)


def test_phon_to_sone_clamps_negative_inverse_to_zero():
    assert phon_to_sone(0.0) >= 0.0


def test_judgment_uses_loudness_max_sone_for_pass_fail(monkeypatch):
    monkeypatch.setattr(dsp, "compute_loudness_trace", lambda *args, **kwargs: ([0.0, 1.0], [5.0, 8.0], 8.0))
    result = engine.judge(config, buffer, sample_rate, mic_cal, audio_file_path)
    assert result.channel_results[0].loudness_max_sone == 8.0
    assert result.channel_results[0].loudness_pass is True


def test_judgment_uses_phon_limits_via_conversion_helpers(monkeypatch):
    monkeypatch.setattr(dsp, "compute_loudness_trace", lambda *args, **kwargs: ([0.0, 1.0], [4.0, 8.0], 8.0))
    result = engine.judge(phon_config, buffer, sample_rate, mic_cal, audio_file_path)
    assert result.channel_results[0].loudness_pass is True


def test_compute_loudness_trace_handles_non_native_sample_rate(monkeypatch):
    times, values, max_sone = compute_loudness_trace(signal, 44100.0)
    assert times
    assert values
    assert max_sone >= 0.0


def test_judgment_marks_loudness_unavailable_when_backend_errors(monkeypatch):
    monkeypatch.setattr(dsp, "compute_loudness_trace", _raising_backend_error)
    result = engine.judge(config, buffer, sample_rate, mic_cal, audio_file_path)
    assert result.channel_results[0].loudness_available is False
    assert result.limit_pass is False
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_dsp.py tests/test_judgment.py -q`

Expected: FAIL because loudness helpers, dataclass fields, and judgment behavior do not exist yet.

- [ ] **Step 3: Add the MoSQITo dependency and DSP loudness helpers**

Implement the smallest DSP surface that the rest of the app can depend on:

```python
def compute_loudness_trace(signal: np.ndarray, sample_rate: float) -> tuple[list[float], list[float], float]:
    ...


def sone_to_phon(value_sone: float) -> float:
    ...


def phon_to_sone(value_phon: float) -> float:
    ...
```

Requirements for this step:
- loudness adapter returns canonical `sone` values only
- loudness adapter also returns the maximum canonical `sone` scalar used by judgment
- sample-rate accommodation or resampling for MoSQITo happens inside this DSP adapter boundary
- conversion helpers own all `sone <-> phon` logic
- no UI-level unit conversion
- negative inverse results clamp to `0.0`

- [ ] **Step 4: Extend judgment dataclasses and loudness evaluation**

Add loudness fields to `ChannelJudgment` and compute:
- `loudness_max_sone`
- `loudness_pass`
- `loudness_available`
- `loudness_error_message`

If loudness computation fails for a selected channel:
- mark that channel unavailable
- make `limit_pass` false
- keep the explicit no-result data in the serialized result

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `pytest tests/test_dsp.py tests/test_frames.py tests/test_judgment.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add requirements.txt app/service/analysis/dsp.py app/service/analysis/frames.py app/service/analysis/judgment.py tests/test_dsp.py tests/test_frames.py tests/test_judgment.py
git commit -m "feat(analysis): add ISO 532 loudness helpers and judgment fields"
```

### Task 2: Persist Loudness Config And Add Config UI Controls

**Files:**
- Modify: `model/channel_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/config_service.py`
- Modify: `ui/modules/config.py`
- Modify: `tests/test_db_connection_service.py`
- Modify: `tests/test_config_service.py`
- Modify: `tests/test_config_module.py`

- [ ] **Step 1: Write failing persistence and UI tests**

Add tests that prove:
- migration adds loudness columns
- migration backfills existing rows with `loudness_unit="sone"`, `loudness_lower_limit=0`, and `loudness_upper_limit=60`
- `ConfigService.load_config()` returns loudness fields
- `ConfigService.save_config()` persists loudness fields
- validation rejects invalid unit, negative bounds, and `lower >= upper`
- `ConfigModule` loads and saves the loudness widgets
- changing `loudness_unit` keeps the numeric bounds visible as-is with no auto-conversion or reset

```python
def test_save_config_persists_loudness_fields():
    ...
    assert loaded["channels"][0]["loudness_unit"] == "phon"


def test_validate_rejects_negative_loudness_limits():
    assert "响度下限" in "\n".join(errors)


def test_config_module_collects_loudness_fields(qapp):
    ...
    assert data["channels"][0]["loudness_upper_limit"] == 60.0
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py -q`

Expected: FAIL because the loudness schema and widgets do not exist yet.

- [ ] **Step 3: Add loudness schema, migration, defaults, and validation**

Implement:
- new model fields on `ChannelConfigTable`
- migration `ALTER TABLE` calls in `DbConnectionService`
- migration backfill of existing rows to `sone / 0 / 60` when loudness columns are newly introduced or null
- `_CHANNEL_DEFAULTS` entries in `ConfigService`
- load/save/validate support for:
  - `loudness_unit`
  - `loudness_lower_limit`
  - `loudness_upper_limit`

- [ ] **Step 4: Add loudness controls to the per-channel config UI**

Place loudness controls alongside other per-channel analysis settings. Keep unit-switch behavior literal:
- changing unit keeps the numeric limits visible
- no auto-conversion
- no reset

Suggested control shape:

```python
combo_loudness_unit = _NoWheelComboBox()
combo_loudness_unit.addItems(["sone", "phon"])

spin_loudness_lo = _NoWheelSpinBox()
spin_loudness_hi = _NoWheelSpinBox()
```

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add model/channel_config_table.py app/service/db_connection_service.py app/service/config_service.py ui/modules/config.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py
git commit -m "feat(config): add per-channel loudness settings"
```

### Task 3: Extend Review Payloads, Result Summary, And Committed Loudness Chart

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `ui/modules/result.py`
- Create: `ui/modules/loudness.py`
- Modify: `tests/test_review_payloads.py`
- Modify: `tests/test_result_module.py`
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Modify: `tests/test_test_manager/test_rejudge.py`
- Modify: `tests/test_test_manager/test_review_payload_emission.py`
- Modify: `tests/test_test_manager/_support.py`
- Modify: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`

- [ ] **Step 1: Write failing payload, result, and chart-module tests**

Add tests that prove:
- summary rows become `SPL / Loudness / FFT / 倍频程 / AI分析`
- loudness deviation formatting uses the chosen channel’s unit
- loudness row goes `无结果` when serialized loudness says unavailable
- `summary_payload["error_message"]` surfaces `loudness_error_message` for constrained loudness no-result states
- committed loudness chart payload is unit-converted and render-ready
- `LoudnessModule` follows the same committed/fallback/empty behavior as SPL
- existing test-manager and factory-controller review payload tests are updated from the old 4-row assumption to the new 5-row contract
- chart recompute failure keeps the serialized loudness judgment authoritative, omits the affected channel's `loudness` payload, and emits a warning log
- chart recompute disagreement logs a warning and does not mutate summary or overall result
- a channel that lacks only `chart_payload[channel]["loudness"]` remains selectable for the shared review surface and yields an empty loudness card instead of a selector fallback

```python
def test_build_summary_payload_inserts_loudness_row_after_spl():
    assert [row["label"] for row in summary["rows"]] == ["SPL", "Loudness", "FFT", "倍频程", "AI分析"]


def test_loudness_module_renders_committed_chart_for_selected_channel(qapp):
    assert module.current_plot_payload()["unit"] == "sone"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_review_payloads.py tests/test_result_module.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -q`

Expected: FAIL because the loudness row, payloads, and module do not exist yet.

- [ ] **Step 3: Extend review-payload and test-manager contracts**

Implement:
- five-row `summary_payload`
- loudness row worst-case selection using canonical `sone`
- loudness constrained no-result states propagate `loudness_error_message` into `summary_payload["error_message"]` and the result UI error area
- chart payload generation for `loudness`
- chart conversion using the committed run-time config snapshot
- serialized loudness judgment remains authoritative if chart recomputation fails or disagrees

- [ ] **Step 4: Add the committed `LoudnessModule` and update `ResultModule`**

Create `ui/modules/loudness.py` by following the committed-chart structure used by `ui/modules/spl.py`.

Requirements:
- review-only rendering
- selected-channel aware
- missing/non-dict `loudness` payload means empty chart
- axis labels:
  - bottom: `Time (s)`
  - left: `Loudness (sone|phon)` based on payload unit

Update `ResultModule` for 5 rows and the new loudness row.

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `pytest tests/test_review_payloads.py tests/test_result_module.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/service/analysis/review_payloads.py app/service/analysis/test_manager.py ui/modules/result.py ui/modules/loudness.py tests/test_review_payloads.py tests/test_result_module.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/_support.py tests/test_factory_mode_controller/test_reanalysis_and_review.py
git commit -m "feat(review): add loudness summary and committed chart"
```

### Task 4: Reflow Main Window Layout And Route Latest Log Line To The Status Bar

**Files:**
- Modify: `app/main_window.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Write failing main-window tests for the new layout and status-bar behavior**

Add tests that prove:
- the system log card is no longer on the test page
- video occupies the old left-bottom slot
- loudness occupies the old video slot
- loudness participates in the shared display-channel selection contract
- a channel that lacks only loudness payload still remains selectable for the other review cards while the loudness card stays empty
- the visible status bar mirrors the latest `bus.log` line by trimming to the last non-empty line
- `bus.status` no longer changes the visible status-bar message
- direct `statusBar().showMessage(...)` startup/user-role text is no longer the visible source of truth

```python
def test_main_window_routes_latest_log_line_to_status_bar(qapp, monkeypatch):
    ...
    services.bus.log.emit("line 1\\nline 2")
    assert window.statusBar().currentMessage() == "line 2"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_main_window.py -q`

Expected: FAIL because the layout, loudness card, and status-bar routing are still unchanged.

- [ ] **Step 3: Reflow the main window and wire the status bar**

Implement:
- remove `LogModule` from the test page layout
- add `LoudnessModule`
- move `VideoModule` into the former left-bottom slot
- keep the existing shared toolbar behavior and extend it to loudness
- connect `bus.log` to a status-bar updater that shows the trimmed last non-empty line
- remove the visible `bus.status -> statusBar().showMessage` path
- stop using direct `statusBar().showMessage(...)` calls as the visible operator surface

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest tests/test_main_window.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main_window.py tests/test_main_window.py
git commit -m "feat(ui): reflow test page for loudness and log status bar"
```

### Task 5: Full Regression Verification For This Feature Slice

**Files:**
- Modify: none expected unless regressions are found
- Test: `tests/test_dsp.py`
- Test: `tests/test_frames.py`
- Test: `tests/test_judgment.py`
- Test: `tests/test_db_connection_service.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_config_module.py`
- Test: `tests/test_review_payloads.py`
- Test: `tests/test_result_module.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Test: `tests/test_main_window.py`
- Test: `tests/test_test_manager/test_rejudge.py`
- Test: `tests/test_test_manager/test_review_payload_emission.py`
- Test: `tests/test_test_manager/_support.py`
- Test: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`

- [ ] **Step 1: Run the full targeted feature regression suite**

Run:

```bash
pytest tests/test_dsp.py tests/test_frames.py tests/test_judgment.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_review_payloads.py tests/test_result_module.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -q
```

Expected: PASS

- [ ] **Step 2: If the targeted suite exposes regressions, fix them in the touched boundaries**

Keep fixes scoped to the loudness/layout feature and rerun the same regression command until it passes cleanly.

- [ ] **Step 3: Commit the final verification-related fixes if any were needed**

```bash
git add requirements.txt model/channel_config_table.py app/service/db_connection_service.py app/service/config_service.py app/service/analysis/dsp.py app/service/analysis/frames.py app/service/analysis/judgment.py app/service/analysis/review_payloads.py app/service/analysis/test_manager.py ui/modules/config.py ui/modules/result.py ui/modules/loudness.py app/main_window.py tests/test_dsp.py tests/test_frames.py tests/test_judgment.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_review_payloads.py tests/test_result_module.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py tests/test_test_manager/test_rejudge.py tests/test_test_manager/test_review_payload_emission.py tests/test_test_manager/_support.py tests/test_factory_mode_controller/test_reanalysis_and_review.py
git commit -m "fix(ui): stabilize loudness analysis integration"
```
