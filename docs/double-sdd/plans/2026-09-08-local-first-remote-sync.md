# Local-first Remote Sync Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile origin/master_0811 into local master_0811 without losing local functionality or unrelated dirty edits.

**Architecture:** Perform a real merge in an isolated worktree, audit all conflicts against equivalent existing fixes, and test the resolved tree. Final integration uses that verified tree with only the original main HEAD and fetched remote HEAD as parents, so remote ancestry is retained without temporary workflow commits.

**Tech Stack:** Git, Python 3.12, PyQt6, existing regression suites.

---

Spec: `docs/double-sdd/specs/2026-09-08-local-first-remote-sync-design.md`.

Recorded base: `MAIN_BASE=c16cfc97b7e16b6a3be9729207cd7e523c2b3caf`; `REMOTE_HEAD=a530bdd09a255a43133d55e7cf43297843117c09`; branch `codex/sync-master-0811-local-first`; worktree `.worktrees/sync-master-0811-local-first`. Main is dirty; never operate on its files from the implementer.

Authoritative pre-integration inventory: `docs/double-sdd/plans/2026-09-08-local-first-protected-files.json`. It records all 14 existing modified/untracked source or plan files, their SHA256 and Git status, captured before implementation. The orchestrator also retains this inventory in session state. Existing unrelated untracked directories (.agents, .codex, .venv, outputs, model_training, packaging/runtime test directories) stay untouched; no recursive filesystem cleanup is permitted against them.

## Task 1: Audit and resolve the complete remote merge

**Owned files:**

- Conflict paths: `app/service/analysis/process_executor.py`, `app/service/analysis/production_executor.py`, `app/service/multi_run_integration/production_composition.py`, `app/service/plc_service.py`, `ui/modules/workorder.py`, `tests/analysis_scheduler/test_production_executor.py`, `tests/multi_run_integration/test_production_composition.py`, `tests/test_workorder_module.py`.
- New evidence document: `docs/double-sdd/plans/2026-09-08-local-first-merge-audit.md`.
- Other files only insofar as Git auto-merges them; inspect any non-conflict change versus MAIN_BASE before accepting it. Do not add functional changes or rewrite tests to make the merge pass.

- [ ] Record task HEAD after document approval. Run `git merge --no-commit --no-ff a530bdd09a255a43133d55e7cf43297843117c09` in isolated worktree; expected RED state is the eight known unmerged paths. Capture `git ls-files -u` and inspect each three-way hunk.
- [ ] Check equivalence: remote `3474c19` vs local `e25e502` code/test patch-id; `e261d00` vs `c16cfc9`; `9a7c5c5` vs `cdb65b6`; `070c1f0` vs `2d55dc3`. Docs-only differences do not mean missing runtime behavior. Compare nonblocking current changes `5db39e4` vs `d21ef86` if needed.
- [ ] Resolve conflict hunks using apply_patch. Preserve local staged analysis timing/summary payload, guarded executor close, resident AI refinements, PLC dispatch/acknowledgement, and current workorder UI. Local baseline may be retained verbatim for a file only after the audit shows remote functionality is equivalent/already present. Never use a blanket ours merge strategy.
- [ ] Record per-file rationale and equivalent-fix evidence in the new audit document. State whether app/ui/tests/requirements contents differ at all from MAIN_BASE. If new source changes are genuinely required, describe them and add the smallest regression reproducer before changing logic.
- [ ] Stage resolved paths, verify `git ls-files -u` empty, `git diff --cached --check` clean, no merge conflict markers in changed files. Existing tests are the behavior oracle for an unchanged source merge; do not fabricate a new failing functional test if no implementation behavior changes.
- [ ] Run isolated suites: `tests/analysis_scheduler/test_production_executor.py`, `tests/multi_run_integration/test_production_composition.py`, `tests/test_workorder_module.py`, `tests/test_plc_service_current.py`, `tests/test_plc_service_workorder.py`, `tests/test_current_meter_service.py`, `tests/resident_audio_mvp/test_stress_and_handoff.py`, `tests/multi_run_integration/test_resident_recovery_production.py`, `tests/test_historical_analysis_dialog.py`, `tests/test_historical_replay_controls.py`. Also locate/run focused process-executor tests if affected by a real content change. Investigate failures against baseline before fixing unrelated code.
- [ ] Commit resolved merge checkpoint only in isolated branch. Report exact tree/commit, original parents, source diff vs MAIN_BASE, tests and audit path; do not merge/push/change main. Orchestrator dispatches paired spec/full-slice quality review against task base and final checkpoint, including explicit merge-source context.

## Final integration (orchestrator)

- [ ] Confirm reviews passed, source tests verified, no unmerged index. Check main still at MAIN_BASE, index empty, original dirty/untracked source hashes unchanged. Do not overwrite newer user work.
- [ ] Obtain verified tree using `git rev-parse <reviewed-head>^{tree}`. Create final merge with `git commit-tree <tree> -p c16cfc97b7e16b6a3be9729207cd7e523c2b3caf -p a530bdd09a255a43133d55e7cf43297843117c09 -m "Merge origin/master_0811 preserving local improvements"`. This avoids retaining temporary worktree commits while fulfilling actual pull ancestry.
- [ ] Before moving main verify final commit parents/tree, then `git merge --ff-only <final-merge-commit>` from main. Git must preserve dirty files; stop rather than force if it refuses. Do not use stash/reset/rebase or ignore conflicts.
- [ ] Run merged-main suites in separate processes: `tests/analysis_scheduler/test_production_executor.py`, `tests/multi_run_integration/test_production_composition.py`, `tests/test_workorder_module.py`, `tests/test_current_meter_service.py`, `tests/test_historical_analysis_dialog.py`. Expected: each exits 0, no failed tests. Verify `git merge-base --is-ancestor REMOTE_HEAD HEAD`, behind=0, final tree equals reviewed tree, original file hashes and working changes preserved. No push/package/start/stop app. Retain worktree until safe cleanup; do not delete ignored test artifacts without inspection.

If any suite fails, rerun its specific failing test on an isolated checkout of MAIN_BASE using the same environment. A failure is baseline-only only when independently reproduced there with the same cause; it must be reported, not silently counted as passing or repaired outside this scope. Merge-introduced failures block integration. Independently confirmed unrelated baseline failures do not justify changing preserved local code; report evidence and require no regression for the local-first history reconciliation.

## Test environment

From the relevant working directory (one Qt suite per process):

```powershell
$env:PATH = 'D:\Program Files\Python\Python312\Lib\site-packages\PyQt6\Qt6\bin;D:\Program Files\Python\Python312\Lib\site-packages\PyQt6;' + $env:PATH
$env:PYTHONPATH = (Get-Location).Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTEST_QT_API = 'pyqt6'
$env:QT_API = 'pyqt6'
$env:QT_QPA_PLATFORM = 'offscreen'
& 'D:\Program Files\Python\Python312\python.exe' -c "import pytest; raise SystemExit(pytest.main(['-q','-p','pytestqt.plugin','tests/test_current_meter_service.py']))"
```

Installed python-snap7 is 3.0.0. Do not change interpreter/dependencies or access user deployment data. Main's unrelated startup compatibility fixes are intentionally absent from the worktree.
