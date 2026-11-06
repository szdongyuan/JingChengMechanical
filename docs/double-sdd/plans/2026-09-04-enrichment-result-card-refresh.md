# Enrichment Result Card Refresh Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the existing result-history card with complete SPL, loudness, and other displayed measurement values when matching Enrichment arrives, without changing Decision/manual judgment or UI layout.

**Architecture:** Build one identity-fenced display overlay in `ResultDashboardModule` that copies only complete chart and displayed measurement data from Enrichment onto the current authoritative Decision/manual payload. Route all history-card payload reads through that overlay and add one in-place payload refresh method to `ResultHistoryModule`, so the same card instance updates immediately and later transitions cannot revert values to `--`.

**Tech Stack:** Python 3.12, PyQt6, existing two-stage Decision/Enrichment pipeline, pytest/pytest-qt

---

## Execution Context

- Main branch at plan time: `master_0811`
- Planned implementation base: `c089ef3` (spec commit) unless main advances before worktree creation; if it advances, record the actual clean committed base and review all intervening changes before implementation.
- Create a dedicated worktree/branch from the recorded main HEAD; never implement in the dirty main worktree.
- Preserve existing uncommitted changes in:
  - `app/service/db_connection_service.py`
  - `app/service/managed_input_sounddevice_backend.py`
  - `main.spec`
  - `tests/resident_audio_mvp/test_ring_capture.py`
  - `tests/test_db_connection_service.py`
  - `tests/test_managed_input_sounddevice_backend.py`
  - all existing untracked local directories/files

## File Structure

- Modify: `ui/modules/result_dashboard.py` — build and retain the Enrichment display overlay; provide overlay-aware payload lookup; request card refresh after accepted Enrichment.
- Modify: `ui/modules/result_history.py` — refresh one existing terminal card in place using an explicit payload, including top metric replay.
- Test: `tests/multi_run_integration/test_two_stage_analysis_projection.py` — end-to-end Decision→Enrichment card refresh, manual ordering, unselected run, idempotency, and lifecycle coverage.
- Test: `tests/test_result_history_module.py` — focused in-place card refresh contract and sone/phon formatting coverage.
- Test: `tests/test_result_history_card.py` only if an existing public card method needs direct characterization; no production change to `ui/widgets/result_history_card.py` is expected.

No changes are planned to recording, DSP, AI, PLC, persistence, schemas, artifact formats, `main.spec`, widget layout, fonts, table structure, or configuration.

## Test Environment

The repository `.venv` lacks pytest. Use the installed Python with its matching Qt DLLs:

```powershell
$env:PATH = "D:\Program Files\Python\Python312\Lib\site-packages\PyQt6\Qt6\bin;D:\Program Files\Python\Python312\Lib\site-packages\PyQt6;" + $env:PATH
$env:PYTHONPATH = (Get-Location).Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:PYTEST_QT_API = "pyqt6"
$env:QT_API = "pyqt6"
$env:QT_QPA_PLATFORM = "offscreen"
```

Invoke pytest with:

```powershell
& "D:\Program Files\Python\Python312\python.exe" -c "import pytest; raise SystemExit(pytest.main(['-p','pytestqt.plugin', <test arguments>]))"
```

## Task 0: Record Base and Create the Isolated Worktree

**Files:**
- No source files

- [ ] **Step 1: Record the current main state**

Run `git branch --show-current`, `git rev-parse HEAD`, and `git status --short` in the main worktree. Record the exact `TASK_BASE`, dirty tracked paths, and untracked paths in the orchestration notes.

- [ ] **Step 2: Confirm scope isolation**

Verify none of the dirty main paths is one of the two planned production UI modules or focused test files. If a planned path is dirty, stop and resolve ownership before proceeding; do not stash or overwrite it.

- [ ] **Step 3: Create the worktree**

Create `codex/enrichment-result-card-refresh` under `.worktrees/enrichment-result-card-refresh` from the recorded `TASK_BASE`. Confirm the new worktree is clean and its HEAD equals `TASK_BASE`.

- [ ] **Step 4: Confirm dirty-main preservation**

Re-run `git status --short` in the main worktree and confirm every pre-existing dirty/untracked entry is unchanged before implementation starts.

## Task 1: Add Enrichment-Aware Result Card Projection

**Files:**
- Modify: `ui/modules/result_dashboard.py`
- Modify: `ui/modules/result_history.py`
- Test: `tests/multi_run_integration/test_two_stage_analysis_projection.py`
- Test: `tests/test_result_history_module.py`

- [ ] **Step 1: Write the failing selected-card refresh test**

Create a production-shaped lightweight Decision summary with the existing six card rows:

- `spl`, `spl_average`, `loudness`, `loudness_average`, and `stft_score` display `--`;
- `ai` contains the authoritative Decision result.

Then publish a matching Enrichment whose summary contains exact values:

- `58.14 dB` SPL peak;
- `53.71 dB` SPL average;
- `11.10 sone` loudness peak;
- `8.08 sone` loudness average;
- one finite STFT score.

Show the dashboard and assert after Enrichment:

- the same `ResultHistoryCard` QWidget instance remains mounted;
- the four table cells and both top average metrics show the exact expected strings;
- STFT score refreshes in its existing row;
- AI row, overall PASS/FAIL, card revision, terminal time, selection and order remain unchanged;
- `decisionPainted` is not emitted a second time.

- [ ] **Step 2: Run the selected-card test and verify RED**

Run only the new test node.

Expected: right-side Enrichment projection succeeds, but the existing result card retains Decision `--` values.

- [ ] **Step 3: Write the failing `ResultHistoryModule` in-place refresh test**

Render one terminal card, retain its object identity and snapshot, then call the planned refresh API with a complete `ReviewDisplayPayload`.

Assert:

- no card is created, deleted, reordered or reselected;
- `ResultHistoryCard.update_snapshot()` updates the existing summary table;
- `_replay_metric_view()` updates “平均声压级/平均响度” from the refreshed summary rather than stale live-cache fallback;
- an active, reanalyzing, unknown or removed run returns `False` without side effects; removal is represented by absence from `_card_entries`, whose ownership lifecycle removes entries before `deleteLater`;
- sone and phon formatting continue to use existing card/history rules.
- the history scroll position and the existing manual-judgment widget parent/attachment remain unchanged.

- [ ] **Step 4: Implement the minimal in-place refresh API**

Add a method such as:

```python
def refresh_terminal_payload(
    self,
    run_id: str,
    payload: ReviewDisplayPayload,
) -> bool:
    entry = self._card_entries.get(run_id)
    if (
        entry is None
        or entry[0].is_active_run
        or entry[0].phase is not ResultPhase.TERMINAL
    ):
        return False
    snapshot, card = entry
    card.update_snapshot(snapshot, payload)
    self._replay_metric_view(snapshot, card, payload)
    card.update()
    return True
```

Use the existing Qt owning-thread call path; do not add a signal, timer, thread, history event, new card, or layout mutation.

- [ ] **Step 5: Implement the identity-fenced display overlay**

In `ResultDashboardModule`:

1. Replace the chart-only merge helper with an Enrichment display-overlay helper.
2. Preserve `current.dashboard_config`.
3. Copy `chart_payload` from matching Enrichment when it is a dict.
4. Retain all valid Enrichment summary rows in the overlay, keyed by `item_key`, so non-card consumers still receive the complete summary. For the existing result card, only these already-rendered measurement rows can become visible:
   - `spl`
   - `spl_average`
   - `loudness`
   - `loudness_average`
   - `stft_score`
5. Override the Enrichment `ai` row with the current Decision `ai` row, and preserve all current `summary_payload` non-row fields, especially `overall_status`, `overall_label`, `error_message`, and manual-fault presentation. Enrichment FFT/octave or other non-card rows remain in the overlay but do not add rows to `ResultHistoryCard`.
6. If current/enrichment summary shape is invalid, keep the current summary and still allow valid chart overlay; do not clear values.
7. Do not mutate either input payload.

Use one overlay-aware lookup for both the selected dashboard and `ResultHistoryModule` payload provider. The provider must:

- begin with `ResultHistoryStore.display_payload(run_id)`;
- apply cached Enrichment only when the current terminal card remains compatible with the existing run/attempt/terminal identity and manual-only revision rules;
- return the original payload when no valid overlay exists.

After accepting a new Enrichment, cache it, build the overlay from the current store payload, call `refresh_terminal_payload(run_id, overlay)` for the existing card even when unselected, and update the right-side projection only when selected.

- [ ] **Step 6: Run the selected-card and module tests and verify GREEN**

Run the exact new node IDs.

Expected: PASS; exact measurement strings appear without card/snapshot replacement.

- [ ] **Step 7: Add transition and lifecycle regressions**

Extend the staged projection integration tests with deterministic cases:

1. Enrichment first, then manual NG/fault-type replacement: card metrics remain complete and only manual overall/fault/revision change.
2. Manual replacement first, then Enrichment: card metrics become complete while manual state stays authoritative.
3. Enrichment updates an unselected old run after a new `ActiveRunChanged`; the old card refreshes immediately and remains complete when selected once.
4. A compatible ordinary history transition, refresh, selection change, or card rebuild after Enrichment still reads the overlay-aware payload and does not revert to `--`.
5. Duplicate, mismatched identity, reanalysis, eviction and close paths do not refresh or retain stale values.

Give those paths separate assertions:

- duplicate replay leaves the already-refreshed exact values visible, does not call the in-place refresh a second time, does not add a cache key, and does not emit `decisionPainted`;
- mismatched identity leaves the current card and existing valid overlay unchanged and does not add/apply a new cache entry;
- reanalysis start removes the old overlay; cancellation does not restore it, while a new attempt may populate only its own values;
- eviction removes the card and all associated overlay/binding state;
- close rejects later delivery and retains no overlay state.

Add a valid-chart/invalid-summary case: the right-side chart updates, while the existing card summary and top metrics remain byte-for-byte/text-for-text unchanged.

Add a recovery case for an accepted Enrichment whose first `refresh_terminal_payload()` call returns `False`: the Enrichment must remain cached/applied. After restoring the card refresh path, trigger a compatible dashboard projection/selection refresh and assert the overlay-aware lookup refreshes the exact values without a second Enrichment event. The implementation must explicitly reapply the selected terminal overlay after compatible snapshot projection; do not rely on `ResultHistoryModule.render_snapshot()` updating a card whose snapshot object is unchanged.

Do not assert only “not `--`”; use the exact formatted values above and inspect the existing object names for table/top metrics.

- [ ] **Step 8: Run focused UI and staged projection suites**

Run:

```text
tests/multi_run_integration/test_two_stage_analysis_projection.py
tests/test_result_history_module.py
tests/test_result_history_card.py
tests/test_result_dashboard_module.py
tests/test_result_dashboard_history_projection.py
```

Expected: all feature-affected tests PASS. If a broader Qt test fails, rerun it independently at the recorded Task Base before classifying it; do not modify unrelated layout code.

- [ ] **Step 9: Commit the task checkpoint**

```powershell
git add ui/modules/result_dashboard.py ui/modules/result_history.py tests/multi_run_integration/test_two_stage_analysis_projection.py tests/test_result_history_module.py tests/test_result_history_card.py
git commit -m "fix: refresh result card after enrichment"
```

Only add `tests/test_result_history_card.py` if it actually changed.

## Task 2: Regression and Scope Verification

**Files:**
- No production files expected

- [ ] **Step 1: Run direct behavior tests**

Repeat Task 1 Step 8 from a clean worktree and record exact results.

- [ ] **Step 2: Run recording and PLC non-regression gates**

Run:

```text
tests/resident_audio_mvp/test_stress_and_handoff.py
tests/test_managed_input_resident_adapter.py
tests/multi_run_integration/test_six_run_pipeline.py
tests/multi_run_integration/test_two_stage_analysis_projection.py
tests/test_plc_service_current.py
tests/test_plc_service_workorder.py
```

Expected: PASS or failures proven unchanged at the recorded Task Base. Confirm Enrichment causes no PLC write and no analysis/persistence call occurs on capture callback or Resident drain threads.

- [ ] **Step 3: Verify the production-shaped artifact evidence**

Use a test fixture matching the observed full artifact topology and exact values. Do not read or modify the user's production database in automated tests. Confirm the overlay displays complete values while preserving current judgment/manual facts.

- [ ] **Step 4: Audit scope and repository state**

Run:

```powershell
git status --short
git diff --check <TASK_BASE>..HEAD
git diff --name-only <TASK_BASE>..HEAD
```

Confirm the implementation range changes only the two planned UI modules and focused tests. Confirm no changes to recording, DSP, AI, PLC, persistence, schema, artifact format, `main.spec`, data, model, outputs, build or unrelated UI layout files.

- [ ] **Step 5: Run full-range quality review**

Dispatch the required `quality-code-reviewer` for `<TASK_BASE>..HEAD`. Keep reviewer findings advisory until reproduced; ignore theoretical bugs and layered validation requests.

- [ ] **Step 6: Finish without disturbing the dirty main worktree**

Use `finishing-a-development-branch`. Do not merge, move, reset or delete user-owned dirty files. Any final integration must preserve the then-current `master_0811` changes and use squash/linear history.

The required skill is available in this repository at `.agents/skills/finishing-a-development-branch/SKILL.md`; read it before completion. If the whole repository test gate is not clean, retain the feature branch/worktree and report the evidence rather than bypassing the skill.
