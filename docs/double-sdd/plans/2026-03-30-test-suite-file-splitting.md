# Test Suite File Splitting Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Execution note:** If a future environment does not expose the `subagent-driven-development` skill by name, follow the equivalent workflow manually: one fresh implementer pass per task, then paired spec-compliance and quality review before advancing.

**Goal:** Replace the four oversized test modules in `tests/` with functionally grouped test packages while preserving pytest discovery, collected coverage, and existing test behavior.

**Architecture:** Capture pre-change pytest inventories first, then replace each oversized test file with a same-topic package containing focused `test_*.py` modules plus local non-collectable support code. Finish by comparing normalized pre/post collected node inventories and rerunning targeted and full-suite pytest commands against the saved baseline.

**Tech Stack:** Python 3.12, pytest, PyQt6, NumPy, PowerShell

---

## File Structure

### Existing files to remove

- `tests/test_factory_mode_controller.py`
- `tests/test_test_manager.py`
- `tests/test_analysis_chart_modules.py`
- `tests/test_stft_module.py`

### Runtime baseline artifacts to create inside the worktree only

- `.codex/tmp/test-suite-file-splitting/pre_collect.txt`
- `.codex/tmp/test-suite-file-splitting/pre_suite.txt`
- `.codex/tmp/test-suite-file-splitting/pre_suite_exitcode.txt`
- `.codex/tmp/test-suite-file-splitting/post_collect.txt`
- `.codex/tmp/test-suite-file-splitting/post_suite.txt`

These files are verification artifacts only. Do not commit them.

### New `test_factory_mode_controller` package

- `tests/test_factory_mode_controller/__init__.py`
- `tests/test_factory_mode_controller/_support.py`
- `tests/test_factory_mode_controller/_support_*.py` only if needed to keep support code below 1000 lines
- `tests/test_factory_mode_controller/test_readiness_and_selection.py`
- `tests/test_factory_mode_controller/test_trigger_routing.py`
- `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- `tests/test_factory_mode_controller/test_reanalysis_and_review.py`
- `tests/test_factory_mode_controller/test_cleanup_and_abort.py`

### New `test_test_manager` package

- `tests/test_test_manager/__init__.py`
- `tests/test_test_manager/_support.py`
- `tests/test_test_manager/_support_fakes.py` only if needed to keep support code below 1000 lines
- `tests/test_test_manager/test_init.py`
- `tests/test_test_manager/test_start_inputs_and_resolution.py`
- `tests/test_test_manager/test_start_acceptance_and_failures.py`
- `tests/test_test_manager/test_stop_behavior.py`
- `tests/test_test_manager/test_rejudge.py`
- `tests/test_test_manager/test_shared_recorder.py`
- `tests/test_test_manager/test_sample_rate_mismatch.py`
- `tests/test_test_manager/test_max_duration.py`
- `tests/test_test_manager/test_late_attach_buffer_slice.py`
- `tests/test_test_manager/test_user_stop_partial_save.py`
- `tests/test_test_manager/test_signal_disconnection.py`
- `tests/test_test_manager/test_review_payload_emission.py`

### New `test_analysis_chart_modules` package

- `tests/test_analysis_chart_modules/__init__.py`
- `tests/test_analysis_chart_modules/conftest.py`
- `tests/test_analysis_chart_modules/_support.py`
- `tests/test_analysis_chart_modules/_support_*.py` only if needed to keep support code below 1000 lines
- `tests/test_analysis_chart_modules/test_runtime_dependency.py`
- `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`

### New `test_stft_module` package

- `tests/test_stft_module/__init__.py`
- `tests/test_stft_module/conftest.py`
- `tests/test_stft_module/_support.py`
- `tests/test_stft_module/_support_*.py` only if needed to keep support code below 1000 lines
- `tests/test_stft_module/test_live_review_rendering.py`
- `tests/test_stft_module/test_review_window_controls.py`
- `tests/test_stft_module/test_selection_box_geometry.py`
- `tests/test_stft_module/test_reanalysis_and_reset.py`

### Responsibility mapping

- `tests/test_factory_mode_controller/_support.py`: shared payload builders, service factories, event helpers, and stubs used across the split controller tests
- `tests/test_test_manager/_support*.py`: shared bus builders, fake session/recorder/monitor helpers, and common payload helpers used across the split manager tests
- `tests/test_analysis_chart_modules/conftest.py`: shared `qapp` fixture for the chart package
- `tests/test_analysis_chart_modules/_support.py`: shared bus/state stubs, chart payload builders, geometry helpers, and empty-chart assertions
- `tests/test_stft_module/conftest.py`: shared `qapp` fixture for the STFT package
- `tests/test_stft_module/_support.py`: shared bus/module construction helpers, review payload builders, seeded history helpers, and selection-box assertions

Support-module rule for all tasks:

- split `_support*.py` by concern as soon as helpers start mixing unrelated responsibilities, even if the file is still below 1000 lines
- use multiple non-collectable support modules instead of one mixed-responsibility helper file when that yields clearer boundaries

## Task 1: Capture Baseline And Split Chart-Focused Tests

**Files:**
- Create: `.codex/tmp/test-suite-file-splitting/pre_collect.txt`
- Create: `.codex/tmp/test-suite-file-splitting/pre_suite.txt`
- Create: `.codex/tmp/test-suite-file-splitting/pre_suite_exitcode.txt`
- Create: `tests/test_analysis_chart_modules/__init__.py`
- Create: `tests/test_analysis_chart_modules/conftest.py`
- Create: `tests/test_analysis_chart_modules/_support.py`
- Create: `tests/test_analysis_chart_modules/_support_*.py` only if needed
- Create: `tests/test_analysis_chart_modules/test_runtime_dependency.py`
- Create: `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- Create: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- Create: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Create: `tests/test_stft_module/__init__.py`
- Create: `tests/test_stft_module/conftest.py`
- Create: `tests/test_stft_module/_support.py`
- Create: `tests/test_stft_module/_support_*.py` only if needed
- Create: `tests/test_stft_module/test_live_review_rendering.py`
- Create: `tests/test_stft_module/test_review_window_controls.py`
- Create: `tests/test_stft_module/test_selection_box_geometry.py`
- Create: `tests/test_stft_module/test_reanalysis_and_reset.py`
- Delete: `tests/test_analysis_chart_modules.py`
- Delete: `tests/test_stft_module.py`

- [ ] **Step 1: Capture the pre-change targeted collect inventory**

Run:

```powershell
New-Item -ItemType Directory -Force .codex/tmp/test-suite-file-splitting | Out-Null
pytest tests/test_factory_mode_controller.py tests/test_test_manager.py tests/test_analysis_chart_modules.py tests/test_stft_module.py --collect-only -q 2>&1 | Tee-Object .codex/tmp/test-suite-file-splitting/pre_collect.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

Expected: exit 0, no collection errors, and collected node ids for the four original files are written to `.codex/tmp/test-suite-file-splitting/pre_collect.txt`.

- [ ] **Step 2: Capture the pre-change full-suite baseline**

Run:

```powershell
pytest tests -q 2>&1 | Tee-Object .codex/tmp/test-suite-file-splitting/pre_suite.txt
$LASTEXITCODE | Set-Content .codex/tmp/test-suite-file-splitting/pre_suite_exitcode.txt
```

Expected: full-suite output is written to `.codex/tmp/test-suite-file-splitting/pre_suite.txt` and the baseline exit code is written to `.codex/tmp/test-suite-file-splitting/pre_suite_exitcode.txt`.

- [ ] **Step 3: Split `test_analysis_chart_modules.py` into the chart package**

Implementation notes:

- move the shared `qapp` fixture into `tests/test_analysis_chart_modules/conftest.py`
- move the bus/state stubs, payload builders, and chart assertions into `tests/test_analysis_chart_modules/_support.py`
- place `test_requirements_txt_is_utf8_and_lists_chart_runtime_dependency` in `tests/test_analysis_chart_modules/test_runtime_dependency.py`
- update its `requirements.txt` lookup for the extra folder nesting depth
- move widget payload, geometry, transform, and marker/error-bar coverage into `test_widget_payload_and_geometry.py`
- move zero-axis and empty-axis coverage into `test_zero_based_axes.py`
- move committed-chart module coverage into `test_committed_chart_modules.py`

- [ ] **Step 4: Split `test_stft_module.py` into the STFT package**

Implementation notes:

- move the shared `qapp` fixture into `tests/test_stft_module/conftest.py`
- move bus/module builders, review payload builders, STFT history seed helpers, and box assertions into `tests/test_stft_module/_support.py`
- move running/analyzing/reviewing render-cache tests into `test_live_review_rendering.py`
- move review-window and analysis-control tests into `test_review_window_controls.py`
- move selection-box edge alignment, clamping, drag, and geometry-unavailable tests into `test_selection_box_geometry.py`
- move reanalysis unlock/reseed/reset tests into `test_reanalysis_and_reset.py`

- [ ] **Step 5: Verify line-count limits for the new chart/STFT files**

Run:

```powershell
$oversized = Get-ChildItem tests/test_analysis_chart_modules, tests/test_stft_module -Recurse -File | Where-Object { $_.Name -match '^(test_.*\.py|_support.*\.py)$' } | ForEach-Object { [PSCustomObject]@{ Path = $_.FullName; Lines = (Get-Content $_.FullName | Measure-Object -Line).Lines } } | Where-Object { $_.Lines -gt 1000 }
$oversized | Format-Table -AutoSize
if ($oversized) { throw 'Chart/STFT split left oversized test or support files.' }
```

Expected: no output and no exception

- [ ] **Step 6: Run the split chart/STFT packages**

Run:

```powershell
pytest tests/test_analysis_chart_modules tests/test_stft_module -q
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add -A tests/test_analysis_chart_modules.py tests/test_stft_module.py tests/test_analysis_chart_modules tests/test_stft_module
git commit -m "test: split chart and stft test modules"
```

## Task 2: Split `test_test_manager.py`

**Files:**
- Create: `tests/test_test_manager/__init__.py`
- Create: `tests/test_test_manager/_support.py`
- Create: `tests/test_test_manager/_support_fakes.py` only if needed
- Create: `tests/test_test_manager/test_init.py`
- Create: `tests/test_test_manager/test_start_inputs_and_resolution.py`
- Create: `tests/test_test_manager/test_start_acceptance_and_failures.py`
- Create: `tests/test_test_manager/test_stop_behavior.py`
- Create: `tests/test_test_manager/test_rejudge.py`
- Create: `tests/test_test_manager/test_shared_recorder.py`
- Create: `tests/test_test_manager/test_sample_rate_mismatch.py`
- Create: `tests/test_test_manager/test_max_duration.py`
- Create: `tests/test_test_manager/test_late_attach_buffer_slice.py`
- Create: `tests/test_test_manager/test_user_stop_partial_save.py`
- Create: `tests/test_test_manager/test_signal_disconnection.py`
- Create: `tests/test_test_manager/test_review_payload_emission.py`
- Delete: `tests/test_test_manager.py`

- [ ] **Step 1: Move shared helpers into support modules**

Implementation notes:

- keep `_DummySignal`, `_FakeSession`, `_factory_runtime_config`, `_make_running_factory_manager`, `_mock_bus`, `_mock_services`, and shared payload helpers in support modules
- move `TestTestManagerInit` into `tests/test_test_manager/test_init.py`
- if a single support module would exceed 1000 lines or starts mixing unrelated helper concerns, split it by concern, for example recorder/session fakes versus assertion/payload helpers

- [ ] **Step 2: Split the `TestTestManagerStartStop` coverage into three focused modules**

Implementation notes:

- `test_start_inputs_and_resolution.py`: config id/name resolution, input/output device identity wiring, monitor-channel selection, config-load failures
- `test_start_acceptance_and_failures.py`: start acceptance ordering, cancellation, recorder/monitor failures, fast-finish and re-entrant error behavior
- `test_stop_behavior.py`: stop-all, stop-by-config, missing-run stop, partial-save cleanup behavior

- [ ] **Step 3: Move the remaining scenario classes to their named modules**

Implementation notes:

- `TestTestManagerRejudge` -> `test_rejudge.py`
- `TestSharedRecorderTeardown` -> `test_shared_recorder.py`
- `TestSampleRateMismatch` -> `test_sample_rate_mismatch.py`
- `TestSharedRecorderMaxDuration` -> `test_max_duration.py`
- `TestLateAttachBufferSlice` -> `test_late_attach_buffer_slice.py`
- `TestUserStopPartialSave` -> `test_user_stop_partial_save.py`
- `TestSignalDisconnection` -> `test_signal_disconnection.py`
- `TestReviewPayloadEmission` -> `test_review_payload_emission.py`

- [ ] **Step 4: Verify line-count limits for the new manager files**

Run:

```powershell
$oversized = Get-ChildItem tests/test_test_manager -Recurse -File | Where-Object { $_.Name -match '^(test_.*\.py|_support.*\.py)$' } | ForEach-Object { [PSCustomObject]@{ Path = $_.FullName; Lines = (Get-Content $_.FullName | Measure-Object -Line).Lines } } | Where-Object { $_.Lines -gt 1000 }
$oversized | Format-Table -AutoSize
if ($oversized) { throw 'Manager split left oversized test or support files.' }
```

Expected: no output and no exception

- [ ] **Step 5: Run the split manager package**

Run:

```powershell
pytest tests/test_test_manager -q
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A tests/test_test_manager.py tests/test_test_manager
git commit -m "test: split test manager module"
```

## Task 3: Split `test_factory_mode_controller.py`

**Files:**
- Create: `tests/test_factory_mode_controller/__init__.py`
- Create: `tests/test_factory_mode_controller/_support.py`
- Create: `tests/test_factory_mode_controller/_support_*.py` only if needed
- Create: `tests/test_factory_mode_controller/test_readiness_and_selection.py`
- Create: `tests/test_factory_mode_controller/test_trigger_routing.py`
- Create: `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- Create: `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- Create: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`
- Create: `tests/test_factory_mode_controller/test_cleanup_and_abort.py`
- Delete: `tests/test_factory_mode_controller.py`

- [ ] **Step 1: Move shared controller helpers into `_support.py`**

Implementation notes:

- move `_run_id_for`, module loaders, payload builders, runtime-state helpers, config/service builders, dummy signal stubs, and selection-setting stubs into `_support.py`
- if `_support.py` becomes oversized or mixes unrelated helper families, split it into clearer `_support_*.py` modules
- keep only file-local helpers in individual test modules if they are truly single-use

- [ ] **Step 2: Split readiness, selection, and trigger coverage**

Implementation notes:

- `test_readiness_and_selection.py`: startup readiness, enabled-selection rules, validation gating, deleted-selection persistence, and config-load recovery
- `test_trigger_routing.py`: PLC and scan routing, blocked trigger emissions, ignored-trigger logging, and deferred trigger refresh cases

- [ ] **Step 3: Split lifecycle, review, and cleanup coverage**

Implementation notes:

- `test_pending_start_lifecycle.py`: pending-start acceptance, stale callback rejection, cancellation, start failures, and blocked-start feedback
- `test_stop_and_active_run.py`: active-run start blocking, selection-change blocking during active phases, stop success/failure/exception handling, and active-run ownership preservation
- `test_reanalysis_and_review.py`: review payload acceptance, reanalysis clearing/reseeding, sparse payload validation, manual review reseeding, and review-state transitions
- `test_cleanup_and_abort.py`: cleanup-settled readiness recomputation, auto-analysis completion/failure gates, runtime abort handling, and real-manager lifecycle bridge cases

- [ ] **Step 4: Verify line-count limits for the new controller files**

Run:

```powershell
$oversized = Get-ChildItem tests/test_factory_mode_controller -Recurse -File | Where-Object { $_.Name -match '^(test_.*\.py|_support.*\.py)$' } | ForEach-Object { [PSCustomObject]@{ Path = $_.FullName; Lines = (Get-Content $_.FullName | Measure-Object -Line).Lines } } | Where-Object { $_.Lines -gt 1000 }
$oversized | Format-Table -AutoSize
if ($oversized) { throw 'Controller split left oversized test or support files.' }
```

Expected: no output and no exception

- [ ] **Step 5: Run the split controller package**

Run:

```powershell
pytest tests/test_factory_mode_controller -q
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A tests/test_factory_mode_controller.py tests/test_factory_mode_controller
git commit -m "test: split factory mode controller module"
```

## Task 4: Compare Inventories And Run Final Verification

**Files:**
- Create: `.codex/tmp/test-suite-file-splitting/post_collect.txt`
- Create: `.codex/tmp/test-suite-file-splitting/post_suite.txt`

- [ ] **Step 1: Capture the post-change targeted collect inventory**

Run:

```powershell
pytest tests/test_factory_mode_controller tests/test_test_manager tests/test_analysis_chart_modules tests/test_stft_module --collect-only -q 2>&1 | Tee-Object .codex/tmp/test-suite-file-splitting/post_collect.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

Expected: exit 0, no collection errors, and collected node ids for the four new packages are written to `.codex/tmp/test-suite-file-splitting/post_collect.txt`.

- [ ] **Step 2: Compare normalized targeted inventories**

Run:

```powershell
$pre = Get-Content .codex/tmp/test-suite-file-splitting/pre_collect.txt | Where-Object { $_ -match '::' } | ForEach-Object { ($_ -split '::') | Where-Object { $_ -like 'test_*' } | Select-Object -Last 1 } | Sort-Object -Unique
$post = Get-Content .codex/tmp/test-suite-file-splitting/post_collect.txt | Where-Object { $_ -match '::' } | ForEach-Object { ($_ -split '::') | Where-Object { $_ -like 'test_*' } | Select-Object -Last 1 } | Sort-Object -Unique
$diff = Compare-Object $pre $post
$diff | Format-Table -AutoSize
if ($diff) { throw 'Normalized targeted test-name inventory changed.' }
```

Expected: no output and no exception

- [ ] **Step 3: Verify the original oversized files are removed**

Run:

```powershell
$legacy = @(
  'tests/test_factory_mode_controller.py',
  'tests/test_test_manager.py',
  'tests/test_analysis_chart_modules.py',
  'tests/test_stft_module.py'
) | Where-Object { Test-Path $_ }
$legacy
if ($legacy) { throw 'Original oversized test files still exist.' }
```

Expected: no output and no exception

- [ ] **Step 4: Run the four split packages together**

Run:

```powershell
pytest tests/test_factory_mode_controller tests/test_test_manager tests/test_analysis_chart_modules tests/test_stft_module -q
```

Expected: PASS

- [ ] **Step 5: Run the full suite baseline comparison**

Run:

```powershell
pytest tests -q 2>&1 | Tee-Object .codex/tmp/test-suite-file-splitting/post_suite.txt
$postExit = $LASTEXITCODE
$preExit = [int](Get-Content .codex/tmp/test-suite-file-splitting/pre_suite_exitcode.txt)
$splitPattern = 'tests/test_factory_mode_controller|tests/test_test_manager|tests/test_analysis_chart_modules|tests/test_stft_module'
$splitFailures = Get-Content .codex/tmp/test-suite-file-splitting/post_suite.txt | Where-Object { $_ -match '^(FAILED|ERROR) ' -and $_ -match $splitPattern }
$splitFailures | Format-Table -AutoSize
if ($splitFailures) { throw 'Full-suite run reported failures inside the split test areas.' }
$pre = Get-Content .codex/tmp/test-suite-file-splitting/pre_suite.txt | Where-Object { $_ -match '^(FAILED|ERROR) ' -and $_ -notmatch $splitPattern }
$post = Get-Content .codex/tmp/test-suite-file-splitting/post_suite.txt | Where-Object { $_ -match '^(FAILED|ERROR) ' -and $_ -notmatch $splitPattern }
$diff = Compare-Object $pre $post
$diff | Format-Table -AutoSize
if ($diff) { throw 'Non-split full-suite failures changed relative to baseline.' }
if ($preExit -ne $postExit) { throw 'Full-suite exit code changed relative to baseline.' }
```

Expected:

- if the baseline suite was green, `pytest tests -q` exits 0, no split-area failures are reported, and `Compare-Object` prints no output
- if the baseline suite had failures outside the four split areas, no split-area failures are reported and `Compare-Object` still prints no output

- [ ] **Step 6: Commit**

```bash
git status --short
```

Expected: only intended tracked changes remain; do not commit the `.codex/tmp/test-suite-file-splitting/` artifacts
