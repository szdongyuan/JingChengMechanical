# Local-first remote merge audit — 2026-09-08

## Inputs and RED evidence

- Isolated worktree: `.worktrees/sync-master-0811-local-first`.
- Task HEAD / checkpoint first parent: `ae25107db9b34f2c98d4ddc3249ef019ad342266`.
- Original main base: `c16cfc97b7e16b6a3be9729207cd7e523c2b3caf`.
- Remote / checkpoint second parent: `a530bdd09a255a43133d55e7cf43297843117c09`.
- Common source ancestor: `b18344cb660945fd99454ba2ffde2d62bbdd65b0`.
- The worktree was clean before `git merge --no-commit --no-ff a530bdd09a255a43133d55e7cf43297843117c09`.
- The merge reported eight content conflicts. `git ls-files -u` contained all three stages for each path (24 entries). This conflicted index is the planned RED state. No new behavior or artificial failing functional test was introduced.

## Equivalent remote fixes

The following values were observed using `git show <commit> -- app ui tests requirements.txt | git patch-id --stable`. Each pair has the same code/test patch ID. The first pair differs in documentation only: local also contains the PLC restoration plan and design. The other four pairs have matching changed-file lists and patch statistics.

| Remote | Existing local | Patch ID | Behavior |
| --- | --- | --- | --- |
| `3474c19` | `e25e502` | `5b79ea1aa14a2f677f54a3e7c6d92eb84605ad2c` | PLC current polling/error handling and per-PLC product model display |
| `e261d00` | `c16cfc9` | `dddfea9db31697b4c7635ac51e155f5169e447ea` | DirectConnection current delivery regression and python-snap7 3.0.0 pin |
| `9a7c5c5` | `cdb65b6` | `7ff46de70aa6054531d2be47c3d0681d02dc1cc9` | Frozen production AI binding, resident request/response, score and review integration |
| `070c1f0` | `2d55dc3` | `74e43b1d7ec0395f08db72f96084113881bdf757` | Resident audio generation recovery, callback isolation and production integration |
| `5db39e4` | `d21ef86` | `fea3a72f9d92fe0ac576eaf7fc0fb63188cd07e9` | Nonblocking current trace failures |

## Per-file three-way resolution

Combined conflict diffs were inspected alongside stage-1-to-stage-3 diffs and the equivalent AI/PLC patches. The 23 conflict hunks were individually resolved with `apply_patch`. Every resolution below retains the inspected local hunk. No merge strategy or checkout command selected an entire side.

### `app/service/analysis/process_executor.py` — 1 hunk

Stage 1 closes only the connection in the finalizer. Remote adds executor shutdown; local already contains shutdown with `executor is not None` protection after staged worker construction. Retain guarded close so shutdown survives construction failure while retaining remote runtime cleanup. Final file equals original main.

### `app/service/analysis/production_executor.py` — 5 hunks

1. Retain `sys`, `uuid`, `zlib`, `ThreadPoolExecutor` and `dataclass` imports required by local bundles and staged DSP. Remote has only the earlier dataclass helper imports.
2. Stage 1 rejects frozen AI. Both branches replace that limitation with the equivalent resident AI patch. Local additionally has bundle ownership/identity/budget checks, feature reuse, parallel feature computation, bundle cleanup, bounded diagnostic bytes and staged decision/detail entry points. Retain the local block; retain the remote-equivalent resident request shape, mapping, timeout, response validation and result projection already present in it and its continuation. Reintroducing remote's old single-stage `execute` block would split the local `_run_frozen_ai` implementation incorrectly.
3. Retain decision/DSP timing completion and resident-AI timing start. Remote has no timing boundaries here.
4. Retain resident-AI timing completion, decision projection timing and extraction of deferred diagnostic buffers. Remote predates those staged fields.
5. Retain `summary_chart_payload` for the decision summary. Remote's `chart_payload` belongs to its eager single-stage implementation; local handles deferred feature completion and the final detail payload separately.

Final file equals original main, including existing resident AI diagnostics, summary behavior and later staged refinements. No unrelated logic was changed.

### `app/service/multi_run_integration/production_composition.py` — 7 hunks

1. Retain `_CaptureSaveLifecycleAuthority`, `_CaptureLifecycleSaver` and `_CaptureLifecycleSaveDispatcher` so active captures remain authoritative through WAV publication and settle on completion/rejection/error. Remote recovery integration does not include this later local lifecycle tracking.
2–4. Retain the optional `ai_toolchain_readiness` parameter, callable validation and stored callback in `_FrozenRunFreezer`. Stage 1 has no AI loader; remote adds the loader already present locally but lacks the later readiness callback.
5. Retain exact frozen-toolchain readiness rejection after copying the loaded toolchain. This composes with the equivalent remote loader, timeout and frozen-envelope changes.
6–7. Retain binding of `analysis_executor.binding_ready` into both captured and imported freezers. Remote supplies only `ai_toolchain_loader`.

Final file equals original main. Existing recovery integration is already the equivalent `2d55dc3` implementation, with later local lifecycle and readiness changes retained.

### `app/service/plc_service.py` — 2 hunks

1. Retain the local `dataclass` import required by disconnect acknowledgement.
2. Retain `_disconnect_acknowledgement`, its property, the dispatcher signal connection and `_execute_dispatch_command` slot. Stage 1 and remote predate local worker-thread dispatch/acknowledgement.

Remote current polling, finite-value validation, read-error deduplication and workorder `model` propagation remain present through equivalent local `e25e502`. Final file equals original main, including shutdown acknowledgement and dispatch support.

### `ui/modules/workorder.py` — 5 hunks

1. Retain Qt, frame and scroll-area imports used by the current local layout.
2. Retain `_rendered_plc_ids = None` as the initial rebuild sentinel. Remote retains the earlier empty tuple.
3–4. Retain `PlcField` object names for SN and model widgets.
5. Omit the duplicated remote insertion into the former two-column grid; retain the local continuation inserting cards by `outer_row` in one column and updating scroll policy.

Stage 1 has SN-only display. Remote adds per-PLC model values/fields/update handling. Those changes already exist locally via `e25e502`; the current layout retains them with local scrolling/separators and styling. Final file equals original main.

### `tests/analysis_scheduler/test_production_executor.py` — 1 hunk

Retain local request/response diagnostic file assertions within the remote-equivalent resident AI success test. Preserve both branches' score, judgement, summary and failure/no-result checks. Stage 1 tested rejection of frozen AI; the equivalent local AI patch already replaced it. Final file equals original main.

### `tests/multi_run_integration/test_production_composition.py` — 1 hunk

Retain `test_captured_freezer_rejects_exact_ai_revision_before_capture_when_unready`. Remote and local both contain the equivalent enabled/disabled AI freezer tests; local adds the exact-revision readiness regression between them. Resident recovery tests auto-merge to the local content. Final file equals original main.

### `tests/test_workorder_module.py` — 1 hunk

Retain assertions for local rows 0–4 and absence of second-column widgets. Remote expects the earlier two-column card layout. Existing SN/model labels, read-only fields, per-PLC updates and lifecycle assertions remain present. Final file equals original main.

## Resulting content and scope

After removing the audited conflict markers, `git diff HEAD --stat` is empty. `git diff c16cfc97b7e16b6a3be9729207cd7e523c2b3caf -- app ui tests requirements.txt` is empty. Thus Git's non-conflict auto-merges also introduce no source changes; no remote fix is missing and no functional RED test or additional process-executor suite is required by the plan's conditional step.

The only tree differences from original main are the approved plan, spec, protected-file inventory and this audit. Main's dirty files, application process and deployment data were not touched. Plan status remains owned by the orchestrator.

## Verification

The resolved index has no unmerged entries (`git ls-files -u` empty). `git diff --cached --check` passes. `rg -n '^(<<<<<<<|=======|>>>>>>>)'` over the eight resolved paths finds no conflict markers. The audit was explicitly added with `git add -f` because repository ignore rules exclude new files under `docs`.

All suites ran in the isolated worktree using the plan's existing system Python/Qt environment. No interpreter, package or dependency changes were made. Each suite ran in its own Python process with this invocation, substituting the exact path listed below for `<suite>`:

```powershell
$env:PATH = 'D:\Program Files\Python\Python312\Lib\site-packages\PyQt6\Qt6\bin;D:\Program Files\Python\Python312\Lib\site-packages\PyQt6;' + $env:PATH
$env:PYTHONPATH = (Get-Location).Path
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTEST_QT_API = 'pyqt6'
$env:QT_API = 'pyqt6'
$env:QT_QPA_PLATFORM = 'offscreen'
& 'D:\Program Files\Python\Python312\python.exe' -c "import pytest; raise SystemExit(pytest.main(['-q','-p','pytestqt.plugin','<suite>']))"
```

| Suite | Observed result | Exit |
| --- | --- | --- |
| `tests/analysis_scheduler/test_production_executor.py` | 29 passed in 8.27s | 0 |
| `tests/multi_run_integration/test_production_composition.py` | 60 passed in 2.76s | 0 |
| `tests/test_workorder_module.py` | 32 passed in 2.07s | 0 |
| `tests/test_plc_service_current.py` | 31 passed in 0.26s | 0 |
| `tests/test_plc_service_workorder.py` | 4 passed in 0.09s | 0 |
| `tests/test_current_meter_service.py` | 52 passed in 3.27s | 0 |
| `tests/resident_audio_mvp/test_stress_and_handoff.py` | 9 passed in 0.38s | 0 |
| `tests/multi_run_integration/test_resident_recovery_production.py` | 14 passed in 0.74s | 0 |
| `tests/test_historical_analysis_dialog.py` | 34 passed in 3.35s | 0 |
| `tests/test_historical_replay_controls.py` | 10 passed in 2.06s | 0 |

Total: 275 passed, no failures, across all 10 planned suites. No baseline investigation or functional refactor was needed. Source content is unchanged, so the plan's conditional additional process-executor tests were not triggered. The merge checkpoint is created only after this verification; final main integration and final two-parent ancestry checks remain the orchestrator's responsibility.
