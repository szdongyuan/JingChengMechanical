# Run Export Progress Reentrancy Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stack overflow during bulk Excel export and prevent stale progress from overwriting terminal state.

**Architecture:** Keep the existing QThread and queued progress signals. Remove the application-level nested event pump from the progress callback and validate current dialog identity around event-processing widget calls. Protect the terminal transition without changing export services or file formats.

**Tech Stack:** Python 3.12, PyQt6, pytest, openpyxl, Windows subprocess isolation.

---

Spec: `docs/double-sdd/specs/2026-09-16-run-export-progress-reentrancy-design.md` (approved).

## Workspace and integration

- [x] Record main branch `master_0811` and immutable main base `42c2a5f297a29a089f8edbc0daa1aade3f02b86d` after the approved spec commit.
- [x] Create branch `codex/fix-export-progress-reentrancy` and worktree `.worktrees/fix-export-progress-reentrancy` from that exact base; confirm `.worktrees/` is ignored.
- [x] Copy changed tracked files plus required untracked app/ui/model/tests/scripts/base/consts dependencies, including the native capture DLL, into the isolated worktree.
- [x] Copy approved spec and plan into the isolated worktree.
- [x] Create temporary snapshot and record TaskBase `c99f6ad0e8c9e0c41fdf8b5511e696f35c50548f`; main dirty files remain untouched. Main source hashes are in `outputs/excel-export-diagnosis-20260916/integration-baseline.json`.

Record main branch and immutable HEAD after the approved specification checkpoint; create `codex/fix-export-progress-reentrancy` at `.worktrees/fix-export-progress-reentrancy`. The main workspace has pre-existing uncommitted code and untracked dependencies. Copy the current changed tracked files and application/test dependency files into the isolated checkout and create a temporary baseline snapshot commit there before implementation. Record that snapshot as TaskBase. Do not merge that snapshot or any temporary commits into main.

Use the main workspace `.venv/Scripts/python.exe` interpreter, with the worktree as cwd so imports resolve to the worktree. Copy spec and this plan into the worktree. Preserve all unrelated edits; final integration is only the TaskBase-to-reviewed-Head patch, checked against current main files before applying. Never overwrite main files with the old committed HEAD. New regression tests must be checked in and not depend on outputs/ diagnostic artifacts.

## File map

- Modify `ui/modules/data_management.py`: only export progress callback and directly necessary terminal-state/identity checks.
- Modify `tests/test_data_management_module.py`: focused deterministic stale/terminal progress checks, existing export compatibility tests.
- Create `tests/test_run_export_progress_reentrancy.py`: parent pytest tests that run real GUI regression scenarios in subprocesses and validate process reports and output files.
- Create `tests/helpers/run_export_progress_probe.py` if a reusable subprocess entry point improves test isolation: synthetic export records, Qt app, real progress dialog/worker/kernel, synchronization and machine-readable diagnostic report. No dependency on developer-machine artifacts or actual business data.

## Task 1: Fix and verify export progress reentrancy (one implementation slice)

Ownership: the file map above. No changes to AI projection, packaging, databases or unrelated UI flows. The implementer is not alone in this repository and must preserve others' changes.

- [x] **Step 1: Establish baseline.** From worktree run `<main>/.venv/Scripts/python.exe -m pytest tests/test_data_management_module.py tests/test_run_record_export_dialog.py -q`. Record result and any environment issue without editing unrelated files.
- [x] **Step 2: Write RED tests.** Build a real Qt subprocess test for 450 synthetic unique records through `_export_selected_rows`, `_ExportWorker`, a real QProgressDialog and `write_run_data_export`. Only mock selection/save dialogs and terminal message boxes. Ensure actual selected IDs match records passed into the export kernel. Use a threading Event handshake to queue a deterministic burst of progress before GUI consumption; no fixed sleep as the sole cause. Include a parent timeout, child faulthandler log and noninteractive Windows fatal-error handling. Test failure must be crash/nonzero exit or observed unbounded nesting, not fixture import/setup failure. Add deterministic tests that deliver stale callbacks after success, failure, cancellation and during widget event processing; confirm old callbacks cannot change a subsequent export window.
- [x] **Step 3: Prove RED.** Run new focused tests against unchanged production code. Store compact failure evidence, including repeated `update_progress` frames. Keep native crashes confined to children so pytest survives. If OS stack thresholds differ, instrument callback nesting and compare growth across input sizes instead of asserting a universal crash threshold.
- [x] **Step 4: Implement the smallest fix.** In `update_progress`, return immediately unless `self._export_progress is progress` and operation is active. Remove its explicit `QApplication.processEvents()`. Check the same identity/terminal condition after widget operations capable of processing events before further writes. Preserve the existing monotonic progress calculation and indeterminate/determinate semantics. `_finish_export` already clears the current progress identity before closing; only adjust it if a failing lifecycle test demonstrates a need. Do not add sleep, timers, a general scheduler, or recursive-limit workarounds. Qt-internal bounded reentrancy is permitted; guard against stale label writes as well as stale terminal writes if nested updates occur.
- [x] **Step 5: Complete real GUI acceptance.** Run 1/450/1000-record XLSX subprocess scenarios and verify N+1 rows, unique identities and restored controls/finished thread. Measure callback depth in the controlled burst and require it to remain bounded as record count grows. For the same 450-record synthetic selection, run all-option ZIP with small valid WAVs/current data and a valid SQLite fixture copy; inspect archive members, parse the inner XLSX and compare shared summary fields (audio paths intentionally differ). Confirm the pipeline is real even if the read service is fixture-backed.
- [x] **Step 6: Validate terminal paths.** Trigger cancellation at a real kernel progress checkpoint before publication; verify canceled status, finished worker, restored controls, existing target bytes intact, temporary stage/files gone. Inject a publication write failure and verify failure with the same cleanup guarantees. Ensure a successful returned result remains successful when cancellation arrives late. Test a second export after the first has completely finished and stale callbacks from the first afterward. Keep test subprocess reports sufficient to distinguish expected cancellation/failure from silent application termination.
- [x] **Step 7: Run focused verification.** `<main>/.venv/Scripts/python.exe -m pytest tests/test_run_export_progress_reentrancy.py tests/test_data_management_module.py tests/test_run_record_export_dialog.py tests/run_record_export tests/run_persistence/test_ai_export_reader.py -q`. Expected all pass. Fix only relevant failures. Supply exact command, counts, RED/GREEN evidence and any platform limitations.
- [x] **Step 8: Checkpoint for review.** Commit only implementation slice files in isolated worktree as `tmp/sdd: fix export progress reentrancy`; report TaskBase and Head and changed files. Orchestrator dispatches independent spec-code-reviewer and quality-code-reviewer on that exact range. Resolve verified current-scope findings, repeat both reviews after code changes.

## Final validation and integration

- [x] Obtain full-slice final quality review (this single task is the complete implementation).
- [x] Read finishing-a-development-branch and verification-before-completion skills; integrate only reviewed delta into main, preserving unrelated dirty changes and excluding temporary snapshot commits.
- [x] Run the focused verification command against integrated main code. Inspect final diff for unrelated changes. Do not build or publish an installer without a user request. Report source fix, test evidence, and that deployed copies still require an updated package.

User's “OK，开始吧” authorizes execution after plan review; do not request another start approval. Future export acceleration remains outside this plan.

## Execution evidence (2026-09-17)

- Implementation checkpoint: `30a58b958df2e0a2e7975723e0fc5e402395c480`. Both independent reviewers PASS on TaskBase..Head; full quality review covers this one-task complete implementation.
- RED: real450 child stopped at depth33, exit86 with repeated update_progress frames; 11 lifecycle tests failed before production fix.
- GREEN: 296 tests passed in isolated worktree; real XLSX depths for1/450/1000 were1/2/2. Both reviewers independently reran296 tests successfully.
- Integrated only reviewed four-file delta into main. Hash comparison verified all unrelated tracked files unchanged and integrated sources equal reviewed worktree. Temporary baseline commits were not merged.
- Integrated main verification: same focused command, **296 passed in 52.36s**; staged diff whitespace check passed. No installer build or deployment performed.
