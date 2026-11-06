# Manual Judgment / Enrichment Race Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep late chart Enrichment attached to its original Decision when the user changes the manual fault type and immediately starts another recording, without overwriting the latest manual result.

**Architecture:** Preserve the immutable Decision-to-Enrichment ownership in the coordinator across manual-only terminal replacements, while continuing to retire it when reanalysis starts. In the dashboard, treat history revision as an ordering cursor rather than the chart identity: accept only the same run/attempt/terminal revision across a provable manual-only revision advance, cache charts for unselected runs, and merge only chart data into the current manual result payload.

**Tech Stack:** Python 3.12, PyQt6, SQLite persistence, pytest/pytest-qt, existing multi-run coordinator and two-stage analysis contracts

---

## File Structure

- `app/service/multi_run_coordinator_mvp/coordinator.py` — retain/rebase pending Decision projection ownership across manual-only replacements; keep reanalysis retirement unchanged.
- `ui/modules/result_dashboard.py` — accept same-Decision Enrichment across manual-only history revision changes and merge charts into the current result overlay.
- `tests/multi_run_coordinator_mvp/test_result_operations.py` — coordinator regression for manual replacement followed by late Enrichment and reanalysis retirement.
- `tests/multi_run_integration/test_two_stage_analysis_projection.py` — full Qt sequencing regressions, including immediate new recording and both event orders.
- `tests/run_persistence/test_analysis_enrichment.py` — durable characterization proving artifact replacement preserves run-level manual facts for readers.

No schema, recording, DSP, AI, PLC, widget layout, artifact format, or public configuration files change.

## Execution Context

- Existing feature worktree: `.worktrees/two-stage-analysis-latency`
- Feature branch: `codex/two-stage-analysis-latency`
- `TASK_BASE`: `741c833c3987be5dcf249fc3069639eba749149d`

Before Task 1, verify `git rev-parse HEAD` equals `TASK_BASE` and record it in the task notes. Every
review, baseline reproduction and final scope audit uses `TASK_BASE..HEAD`.

## Test Environment

The repository `.venv` does not contain pytest. Use the installed Python with its matching PyQt6 DLL path:

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

## Task 1: Preserve Coordinator Enrichment Ownership Across Manual Replacement

**Files:**
- Modify: `app/service/multi_run_coordinator_mvp/coordinator.py`
- Test: `tests/multi_run_coordinator_mvp/test_result_operations.py`
- Test: `tests/multi_run_integration/test_two_stage_analysis_projection.py`

- [ ] **Step 1: Write the failing coordinator regression**

Build a successful automatic Decision so `_decision_projections` owns `(run_id, attempt_id, terminal_revision)`. Apply a manual NG judgment with a fault type, then pass the original matching `EnrichmentCommittedEvent` to `project_enrichment()`.

Assert:

- manual persistence/history still occurs exactly once and emits no PLC or analysis action;
- the Decision projection remains eligible after `TERMINAL_RESULT_REPLACED`;
- matching Enrichment produces exactly one `ResultEnrichmentProjection` rather than `None`;
- a duplicate Enrichment remains ignored.

Use two barrier-controlled cases rather than one nondeterministic assertion:

- block the manual history projector/sink before delivery, call `project_enrichment()`, and assert the projection carries the exact original Decision revision;
- let the manual history event finish projection/enqueue first, then call `project_enrichment()`, and assert the projection carries the exact new manual history revision.

- [ ] **Step 2: Write the failing reanalysis retirement regression**

From the same initial state, start reanalysis before Enrichment. Cancel reanalysis and then deliver the old attempt's Enrichment. Assert `project_enrichment()` returns `None`; cancellation does not recreate old Decision ownership.

- [ ] **Step 3: Run the new tests and verify RED**

Run the exact new test node IDs with the test environment above.

Expected: the manual-first case fails because `TERMINAL_RESULT_REPLACED` currently removes `_decision_projections`; the existing reanalysis behavior remains green.

- [ ] **Step 4: Implement the minimal ownership change**

In `_dispatch_result_history()`:

- retain `_DecisionProjectionState` for `TERMINAL_RESULT_REPLACED`;
- continue removing it immediately for `REANALYSIS_STARTED`;
- after a manual history event is successfully projected/enqueued, advance the same immutable state's `history_revision` to `event.proposed_revision` only if run/attempt/terminal identity still matches and it has not been retired/replaced;
- do not hold the coordinator lock while invoking external projector or sink code;
- allow the existing race in which Enrichment already captured the prior revision; Task 2 handles that exact queued ordering without weakening non-manual fences.

Do not add a retry loop, new thread, PLC action, terminal fact, or persistence mutation.

- [ ] **Step 5: Run coordinator and staged projection tests**

Run:

```text
tests/multi_run_coordinator_mvp/test_result_operations.py
tests/multi_run_integration/test_two_stage_analysis_projection.py
```

Expected: PASS apart from any explicitly documented failure that reproduces unchanged at
`741c833c3987be5dcf249fc3069639eba749149d`.

- [ ] **Step 6: Commit the task checkpoint**

```powershell
git add app/service/multi_run_coordinator_mvp/coordinator.py tests/multi_run_coordinator_mvp/test_result_operations.py tests/multi_run_integration/test_two_stage_analysis_projection.py
git commit -m "fix: retain enrichment across manual judgment"
```

## Task 2: Merge Late Charts Into the Latest Manual Result

**Files:**
- Modify: `ui/modules/result_dashboard.py`
- Test: `tests/multi_run_integration/test_two_stage_analysis_projection.py`
- Test: `tests/run_persistence/test_analysis_enrichment.py`
- Test: `tests/test_result_dashboard_module.py` only if a focused helper belongs there

- [ ] **Step 1: Write the exact user-sequence Qt regression**

Use real `HistoryQtAdapter` mapping and a shown `ResultDashboardModule`:

1. publish a terminal Decision with `chart_payload={}`;
2. publish the committed manual NG/fault-type replacement;
3. publish `ActiveRunChanged` for a new recording so follow-latest selects it;
4. publish the old run's matching Enrichment;
5. select the old run once.

Assert the chart is immediately populated, the card remains the manual NG with the chosen fault type/revision, no second manual action is needed, and Decision paint acknowledgement is not duplicated.

- [ ] **Step 2: Add event-order and repeated-manual regressions**

Cover:

- manual replacement before Enrichment while the old run remains selected: charts appear automatically;
- Enrichment before manual replacement: charts remain after the manual result arrives;
- two manual fault changes before Enrichment: only the latest manual conclusion/fault remains and charts apply once;
- a queued old Enrichment after reanalysis start and cancellation remains rejected. A restored cancelled card is not a manual-only revision advance because its `manual_judgment_revision` does not equal its new card revision;
- after a successful new reanalysis Decision, that new attempt's matching Enrichment is accepted and displayed, while the old attempt remains rejected;
- repeat new-recording selection changes, then evict the old run by exceeding `ResultHistoryStore.MAX_TERMINAL_RESULTS`; assert all cached/applied Enrichment and Decision-binding state for that run is removed. Extend the existing `test_eviction_and_close_purge_all_staged_projection_payloads` pattern rather than adding a second cleanup mechanism.

- [ ] **Step 3: Add the durable reader characterization**

Using the existing persistence fixtures, commit Decision, apply manual judgment, then commit Enrichment artifact replacement. Read through the production result-control/detail/export reader boundary and assert:

- the attempt payload now contains complete charts;
- run-level `judgment_source`, latest judgment, fault type, and manual revision are unchanged;
- the reader combines those manual facts with the full artifact without writing a second terminal or PLC action.

This test may already pass before the UI fix; it protects an existing invariant rather than forcing a persistence code change.

- [ ] **Step 4: Run the user-sequence test and verify RED**

Run the new Qt test node IDs before implementation.

Expected: the original manual-first/new-recording sequence fails because the dashboard requires `target.revision == expected_revision` and ties cached Enrichment to the old history revision.

- [ ] **Step 5: Implement chart-only overlay merging**

In `ResultDashboardModule`:

- keep the validated immutable projection identity `(run_id, attempt_id, terminal_revision)` separate from the mutable history revision;
- retain the Decision origin revision for ordering diagnostics, but key applied/cached Enrichment by immutable projection identity rather than manual card revision;
- accept revision drift only when the target is terminal, the immutable identity still matches, and the current card proves a manual-only replacement with `manual_judgment_revision == card.revision` and a revision not older than the Decision/event cursor;
- continue requiring exact revision for non-manual terminal cards;
- reject reanalyzing, replaced-attempt, mismatched, evicted, closed, and duplicate events as before;
- merge only `chart_payload` and strictly chart-display data from Enrichment into the current `ReviewDisplayPayload`; preserve the current card/manual summary, fault type, judgment and manual revision;
- retain the merged chart payload for an unselected old run and apply it when that run is selected;
- do not change visible widget layout or add user-facing states.

Do not add content-revision revalidation in the UI: the persistence boundary already validates the full `AnalysisBundleIdentity`, including content revision, before the coordinator derives this UI projection.

- [ ] **Step 6: Run focused UI, persistence, and operation suites**

Run:

```text
tests/multi_run_integration/test_two_stage_analysis_projection.py
tests/multi_run_coordinator_mvp/test_result_operations.py
tests/run_persistence/test_analysis_enrichment.py
tests/multi_run_integration/test_history_qt_adapter.py
tests/test_result_dashboard_module.py
tests/test_result_dashboard_history_projection.py
```

Expected: all feature-affected tests PASS. If a broader test fails, reproduce it at Task Base before classifying it as unrelated.

- [ ] **Step 7: Run recording and PLC non-regression gates**

Run the existing recording-integrity/overlap tests named by the two-stage analysis plan plus the PLC staged projection tests. Confirm no new analysis/persistence work occurs on capture callback or Resident drain threads and no Enrichment emits a PLC write.

Exact command inputs:

```text
tests/resident_audio_mvp/test_stress_and_handoff.py
tests/test_managed_input_resident_adapter.py
tests/multi_run_integration/test_six_run_pipeline.py
tests/multi_run_integration/test_two_stage_analysis_projection.py
```

Expected: all tests in these files PASS. The last file contains the staged PLC no-duplicate-write assertions.

- [ ] **Step 8: Commit the task checkpoint**

```powershell
git add ui/modules/result_dashboard.py tests/multi_run_integration/test_two_stage_analysis_projection.py tests/run_persistence/test_analysis_enrichment.py tests/test_result_dashboard_module.py
git commit -m "fix: retain late charts after manual result"
```

## Task 3: Final Verification and Scope Audit

**Files:**
- No production files expected

- [ ] **Step 1: Run all directly affected suites**

Repeat Task 2 Steps 6–7 from a clean worktree and record exact pass/fail counts.

- [ ] **Step 2: Verify the original reproduction end to end**

Run the deterministic shown-widget sequence without a second manual click. Confirm the old run shows complete charts after one selection and retains the first chosen fault type.

- [ ] **Step 3: Audit repository scope**

Run:

```powershell
git status --short
git diff --check 741c833c3987be5dcf249fc3069639eba749149d..HEAD
git diff --name-only 741c833c3987be5dcf249fc3069639eba749149d..HEAD
```

Confirm there are no changes to recording, DSP, AI, PLC, schema, widget layout, `main.spec`, data, model, build, or unrelated files.

- [ ] **Step 4: Hand off to full-range review**

Run the required full-range `quality-code-reviewer` for this bug-fix range. Keep the existing feature worktree/branch; do not integrate temporary branch commits into main unless the whole parent performance branch satisfies its existing completion gate.
