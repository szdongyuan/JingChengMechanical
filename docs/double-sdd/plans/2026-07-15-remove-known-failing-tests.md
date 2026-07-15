# Remove Known Failing Tests Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete exactly the 35 approved failing pytest cases while preserving production code and every other collected test.

**Architecture:** Treat the current full-suite node-ID set as the preservation boundary. Capture it before editing, remove only the named tests from the approved spec, then prove that the post-edit collection is exactly the baseline set minus those 35 cases. Keep all changes inside the three retained test files plus deletion of the all-failing seed test file.

**Tech Stack:** Python 3.12, pytest, PyQt6/pytest-qt, PowerShell, Git

---

## Source of Truth

- Spec: `docs/double-sdd/specs/2026-07-15-remove-known-failing-tests-design.md`
- Working branch: `codex/session-capture-warmup-20260714`
- Dedicated worktree: `.worktrees/session-capture-warmup-20260714`
- Approved spec commit: `077fb20`

## File Structure

- Modify: `tests/test_main_window.py`
  - Remove the 24 approved functions/25 collected cases.
  - Preserve the five named Card tests.
  - Remove only test-local imports/helpers proven unused after deletion.
- Delete: `tests/test_seed_historical_normal_report_data.py`
  - All three tests in the file are approved for deletion.
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - Remove only the two approved stale axis-label tests.
- Modify: `tests/test_stft_module/test_selection_box_geometry.py`
  - Remove only the five approved geometry tests.
- Do not modify any production, shared fixture, shared support, configuration, dependency, or unrelated test file.

## Task 1: Delete the Exact Approved Test Set

**Files:**

- Modify: `tests/test_main_window.py`
- Delete: `tests/test_seed_historical_normal_report_data.py`
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Modify: `tests/test_stft_module/test_selection_box_geometry.py`
- Reference: `docs/double-sdd/specs/2026-07-15-remove-known-failing-tests-design.md`

- [ ] **Step 1: Confirm the implementation boundary and capture the baseline node-ID manifest**

Work from the dedicated worktree. Before touching any test file, verify the branch and cleanliness:

```powershell
git branch --show-current
git status --short
git rev-parse HEAD
```

Expected: branch `codex/session-capture-warmup-20260714` and an empty `git status --short` result. The orchestrator must commit the approved plan before dispatching implementation.

Capture the full baseline collection outside the repository under a unique temporary directory. Use this exact environment for both manifests:

```powershell
$python = (Get-Command python -ErrorAction Stop).Source
$workdir = (Resolve-Path -LiteralPath '.').Path
$qtPlatform = 'offscreen'
$pytestQtApi = 'pyqt6'
$env:QT_QPA_PLATFORM = $qtPlatform
$env:PYTEST_QT_API = $pytestQtApi
$manifestRoot = Join-Path $env:TEMP ("jingcheng-remove-tests-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $manifestRoot | Out-Null
$baselinePath = Join-Path $manifestRoot 'baseline-nodeids.txt'
$postPath = Join-Path $manifestRoot 'post-nodeids.txt'
$approvedFunctionNamesPath = Join-Path $manifestRoot 'approved-function-names.txt'
$contextPath = Join-Path $env:TEMP 'jingcheng-remove-known-failing-tests-context.json'

function Convert-ToNormalizedNodeId([string] $line) {
    if (-not $line.Contains('::')) { return $null }
    $parts = $line -split '::', 2
    $pathPart = $parts[0].Replace('\', '/')
    $testsIndex = $pathPart.IndexOf('tests/')
    if ($testsIndex -lt 0) { return $null }
    return $pathPart.Substring($testsIndex) + '::' + $parts[1]
}

$rawBaseline = @(& $python -m pytest --collect-only -q)
if ($LASTEXITCODE -ne 0) { throw 'Baseline pytest collection failed' }
$baselineNodes = @(
    $rawBaseline |
        ForEach-Object { Convert-ToNormalizedNodeId $_ } |
        Where-Object { $null -ne $_ } |
        Sort-Object -Unique
)
$baselineNodes | Set-Content -LiteralPath $baselinePath -Encoding utf8
if ($baselineNodes.Count -ne 2804) {
    throw "Expected the observed 2804-case baseline, got $($baselineNodes.Count)"
}

[pscustomobject]@{
    Python = $python
    Workdir = $workdir
    QtPlatform = $qtPlatform
    PytestQtApi = $pytestQtApi
    BaselinePath = $baselinePath
    PostPath = $postPath
    ApprovedFunctionNamesPath = $approvedFunctionNamesPath
} | ConvertTo-Json | Set-Content -LiteralPath $contextPath -Encoding utf8
```

Do not add the context file or either manifest to Git. Later steps must reload this JSON file rather than relying on process-local variables.

- [ ] **Step 2: Delete only the approved tests**

Read the spec's `Exact Deletion Set` section in full, then:

1. Remove the 24 named MainWindow functions. The parameterized role test accounts for two collected cases.
2. Delete `tests/test_seed_historical_normal_report_data.py` entirely.
3. Remove the two named axis-label tests.
4. Remove the five named STFT geometry tests.

Use `apply_patch` for modifications and file deletion. Do not use a formatter or bulk rewrite that changes retained test bodies.

- [ ] **Step 3: Remove only mechanically dead test-local scaffolding**

The axis-label and STFT geometry deletions do not make any imports or helpers unused; do not clean anything else from those two files.

In `tests/test_main_window.py`, remove exactly this now-dead MainWindow scaffolding:

- imports: `dataclasses`, `sys`, `ModuleType`, `SimpleNamespace`, `MagicMock`, `numpy`, `QCloseEvent`, `QLabel`, `StftFrame`, `DEFAULT_RESULT_DASHBOARD_LAYOUT`, `FactoryControllerState`, `FactoryReviewPayload`, `FactoryReviewWindow`, and `app.main_window as main_window_mod`;
- the `cv2` and `snap7` module-stubbing block;
- definitions: `_SignalStub`, `_BusStub`, `_StubModule`, `_make_guarded_module`, `_make_guarded_ai_tool_management_module`, `_ChannelProbeModule`, `_RefreshProbeResultModule`, `_FakeLoginWindow`, `_patch_non_target_modules`, `_make_services`, `_make_window`, and `_show`.

Keep the existing `qapp` and `_close_top_level_widgets` fixtures and their helpers. Keep imports required by those fixtures and the five protected Card tests: `gc`, `os`, `pytest`, `sip`, `QCoreApplication`, `QEvent`, `Qt`, `QApplication`, `QWidget`, `GraphicsView`, `APP_QSS`, and `Card`.

Do not edit:

- `tests/conftest.py`
- `tests/test_stft_module/_support.py`
- any other test file
- anything under `app/`, `ui/`, `model/`, `scripts/`, or `consts/`

Verify the touched retained files compile:

```powershell
python -m py_compile `
    tests/test_main_window.py `
    tests/test_analysis_chart_modules/test_committed_chart_modules.py `
    tests/test_stft_module/test_selection_box_geometry.py
```

Expected: exit code 0.

Verify the exact dead symbols are absent:

```powershell
$deadSymbols = @(
    '_SignalStub', '_BusStub', '_StubModule', '_make_guarded_module',
    '_make_guarded_ai_tool_management_module', '_ChannelProbeModule',
    '_RefreshProbeResultModule', '_FakeLoginWindow',
    '_patch_non_target_modules', '_make_services', '_make_window', '_show'
)
foreach ($symbol in $deadSymbols) {
    $matches = @(rg -n "^(class|def) $([regex]::Escape($symbol))\b" tests/test_main_window.py)
    if ($matches.Count -ne 0) { throw "Dead symbol remains: $symbol" }
}

$deadImportPatterns = @(
    '^import dataclasses$', '^import sys$',
    '^from types import ModuleType, SimpleNamespace$',
    '^from unittest.mock import MagicMock$', '^import numpy as np$',
    '^from PyQt6\.QtGui import QCloseEvent$',
    '^from PyQt6\.QtWidgets import .*QLabel',
    '^from app\.service\.analysis\.frames import StftFrame$',
    '^from app\.service\.result_dashboard_layout import DEFAULT_RESULT_DASHBOARD_LAYOUT$',
    '^from app\.services_container import \($',
    '^import app\.main_window as main_window_mod$'
)
foreach ($pattern in $deadImportPatterns) {
    $matches = @(rg -n $pattern tests/test_main_window.py)
    if ($matches.Count -ne 0) { throw "Dead import remains for pattern: $pattern" }
}
$stubBlockMatches = @(
    rg -n 'sys\.modules\.setdefault\("cv2"|if "snap7" not in sys\.modules' tests/test_main_window.py
)
if ($stubBlockMatches.Count -ne 0) { throw 'cv2/snap7 test stubbing block remains' }
```

- [ ] **Step 4: Prove the collection delta is exactly the approved 35 cases**

Run the following as one complete PowerShell block. It reloads the persisted context in a new shell, reapplies the exact environment, verifies the working directory, compares the full node-ID sets, and performs the file-level checks without relying on variables from any other block:

```powershell
$contextPath = Join-Path $env:TEMP 'jingcheng-remove-known-failing-tests-context.json'
if (-not (Test-Path -LiteralPath $contextPath)) { throw 'Collection context is missing' }
$context = Get-Content -LiteralPath $contextPath -Raw | ConvertFrom-Json
if ((Resolve-Path -LiteralPath '.').Path -ne $context.Workdir) {
    throw 'Post-deletion collection is not running from the baseline workdir'
}
if (-not (Test-Path -LiteralPath $context.Python)) { throw 'Recorded Python executable is missing' }
if (-not (Test-Path -LiteralPath $context.BaselinePath)) { throw 'Baseline manifest is missing' }

$env:QT_QPA_PLATFORM = $context.QtPlatform
$env:PYTEST_QT_API = $context.PytestQtApi
$python = $context.Python

function Convert-ToNormalizedNodeId([string] $line) {
    if (-not $line.Contains('::')) { return $null }
    $parts = $line -split '::', 2
    $pathPart = $parts[0].Replace('\', '/')
    $testsIndex = $pathPart.IndexOf('tests/')
    if ($testsIndex -lt 0) { return $null }
    return $pathPart.Substring($testsIndex) + '::' + $parts[1]
}

$baselineNodes = @(Get-Content -LiteralPath $context.BaselinePath)
$rawPost = @(& $python -m pytest --collect-only -q)
if ($LASTEXITCODE -ne 0) { throw 'Post-deletion pytest collection failed' }
$postNodes = @(
    $rawPost |
        ForEach-Object { Convert-ToNormalizedNodeId $_ } |
        Where-Object { $null -ne $_ } |
        Sort-Object -Unique
)
$postNodes | Set-Content -LiteralPath $context.PostPath -Encoding utf8

$removedNodes = @(
    Compare-Object -ReferenceObject $baselineNodes -DifferenceObject $postNodes |
        Where-Object SideIndicator -eq '<=' |
        ForEach-Object InputObject
)
$addedNodes = @(
    Compare-Object -ReferenceObject $baselineNodes -DifferenceObject $postNodes |
        Where-Object SideIndicator -eq '=>' |
        ForEach-Object InputObject
)

if ($removedNodes.Count -ne 35) {
    throw "Expected exactly 35 removed node IDs, got $($removedNodes.Count)"
}
if ($addedNodes.Count -ne 0) {
    throw "Unexpected added node IDs: $($addedNodes -join ', ')"
}
if ($postNodes.Count -ne 2769) {
    throw "Expected 2769 retained node IDs, got $($postNodes.Count)"
}

# Compare against the explicit approved set, including both exact parameter IDs.
$approvedRemovedNodeIds = @(
    'tests/test_main_window.py::test_show_change_password_window_when_logged_out_updates_visible_status_via_log',
    'tests/test_main_window.py::test_main_window_includes_ai_tool_management_tab_in_topbar',
    'tests/test_main_window.py::test_engineer_role_can_access_ai_tool_management_tab',
    'tests/test_main_window.py::test_admin_role_can_access_ai_tool_management_tab',
    'tests/test_main_window.py::test_operator_role_cannot_access_ai_tool_management_tab',
    'tests/test_main_window.py::test_no_role_programmatic_page_reentry_remains_allowed',
    'tests/test_main_window.py::test_operator_cannot_activate_ai_tool_management_via_programmatic_tab_change',
    'tests/test_main_window.py::test_switching_away_from_ai_tool_management_is_blocked_when_confirm_leave_rejects',
    'tests/test_main_window.py::test_programmatic_tab_change_reverts_when_ai_tool_management_rejects_leave',
    'tests/test_main_window.py::test_switching_away_from_config_is_blocked_when_confirm_leave_rejects',
    'tests/test_main_window.py::test_switching_away_from_config_is_allowed_when_confirm_leave_accepts',
    'tests/test_main_window.py::test_role_switch_from_admin_to_operator_stays_consistent_when_ai_leave_is_rejected',
    'tests/test_main_window.py::test_closing_main_window_is_blocked_when_ai_tool_management_rejects_leave',
    'tests/test_main_window.py::test_closing_main_window_is_blocked_when_config_rejects_leave',
    'tests/test_main_window.py::test_admin_and_engineer_roles_can_use_plc_settings_card[\u7ba1\u7406\u5458]',
    'tests/test_main_window.py::test_admin_and_engineer_roles_can_use_plc_settings_card[\u5de5\u7a0b\u5e08]',
    'tests/test_main_window.py::test_closing_main_window_stops_plc_cleanup_before_shared_shutdown_and_camera',
    'tests/test_main_window.py::test_closing_main_window_uses_shared_shutdown_boundary_and_stops_camera',
    'tests/test_main_window.py::test_closing_main_window_still_uses_shared_shutdown_and_camera_when_plc_cleanup_raises',
    'tests/test_main_window.py::test_closing_main_window_swallows_camera_cleanup_errors_after_shared_shutdown',
    'tests/test_main_window.py::test_closing_main_window_still_stops_camera_when_shared_shutdown_raises',
    'tests/test_main_window.py::test_main_window_uses_result_dashboard_module_for_right_side',
    'tests/test_main_window.py::test_vibration_panel_titles_use_parentheses',
    'tests/test_main_window.py::test_main_window_keeps_fixed_left_column_modules_unchanged',
    'tests/test_main_window.py::test_main_window_routes_latest_log_line_to_status_bar',
    'tests/test_seed_historical_normal_report_data.py::test_seed_mock_normal_report_data_creates_reusable_normal_distribution_rows',
    'tests/test_seed_historical_normal_report_data.py::test_seed_mock_normal_report_data_is_idempotent_for_prefix',
    'tests/test_seed_historical_normal_report_data.py::test_seed_mock_normal_report_data_can_clear_all_report_rows',
    'tests/test_analysis_chart_modules/test_committed_chart_modules.py::test_spectrum_module_keeps_chinese_axis_labels_visible',
    'tests/test_analysis_chart_modules/test_committed_chart_modules.py::test_stft_module_keeps_chinese_axis_labels_visible',
    'tests/test_stft_module/test_selection_box_geometry.py::test_selection_box_height_matches_plot_area_and_does_not_extend_below_it',
    'tests/test_stft_module/test_selection_box_geometry.py::test_single_sample_stft_selection_box_uses_one_frame_tolerance_formula',
    'tests/test_stft_module/test_selection_box_geometry.py::test_move_selection_to_start_clamps_to_captured_plot_intersection',
    'tests/test_stft_module/test_selection_box_geometry.py::test_preseeded_selection_box_render_clamps_to_latest_valid_start_inside_captured_plot_intersection',
    'tests/test_stft_module/test_selection_box_geometry.py::test_dragging_passive_selection_box_does_not_move_selected_window'
)
$nodeIdMismatch = @(
    Compare-Object `
        -ReferenceObject ($approvedRemovedNodeIds | Sort-Object) `
        -DifferenceObject ($removedNodes | Sort-Object)
)
if ($nodeIdMismatch.Count -ne 0) {
    throw "Removed node-ID set differs from approval: $($nodeIdMismatch | Out-String)"
}

$approvedFunctionNames = @(
    $approvedRemovedNodeIds |
        ForEach-Object { (($_ -split '::')[-1]) -replace '\[.*\]$', '' } |
        Sort-Object -Unique
)
$approvedFunctionNames |
    Set-Content -LiteralPath $context.ApprovedFunctionNamesPath -Encoding utf8

# Re-collect the two mixed-result files with the recorded Python and environment.
$rawMainWindow = @(& $python -m pytest tests/test_main_window.py --collect-only -q)
if ($LASTEXITCODE -ne 0) { throw 'MainWindow collection failed' }
$mainWindowNodes = @(
    $rawMainWindow |
        ForEach-Object { Convert-ToNormalizedNodeId $_ } |
        Where-Object { $null -ne $_ } |
        Sort-Object -Unique
)
$protectedMainWindowNodes = @(
    'tests/test_main_window.py::test_card_compact_dashboard_mode_reduces_layout_chrome',
    'tests/test_main_window.py::test_card_title_suffix_does_not_overlap_title_at_narrow_width',
    'tests/test_main_window.py::test_card_title_metric_uses_score_value_box_style',
    'tests/test_main_window.py::test_dashboard_compact_card_qss_removes_card_chrome',
    'tests/test_main_window.py::test_card_empty_title_suffix_is_hidden_and_title_uses_row_width'
)
$mainWindowMismatch = @(
    Compare-Object `
        -ReferenceObject ($protectedMainWindowNodes | Sort-Object) `
        -DifferenceObject $mainWindowNodes
)
if ($mainWindowMismatch.Count -ne 0) {
    throw "Protected MainWindow collection mismatch: $($mainWindowMismatch | Out-String)"
}

$rawStftGeometry = @(
    & $python -m pytest tests/test_stft_module/test_selection_box_geometry.py --collect-only -q
)
if ($LASTEXITCODE -ne 0) { throw 'STFT geometry collection failed' }
$stftGeometryNodes = @(
    $rawStftGeometry |
        ForEach-Object { Convert-ToNormalizedNodeId $_ } |
        Where-Object { $null -ne $_ } |
        Sort-Object -Unique
)
if ($stftGeometryNodes.Count -ne 19) {
    throw "Expected 19 retained STFT geometry nodes, got $($stftGeometryNodes.Count)"
}
if (Test-Path -LiteralPath 'tests/test_seed_historical_normal_report_data.py') {
    throw 'Seed historical report test file still exists'
}
```

Expected: exactly 5 and 19 tests respectively, with the five protected MainWindow test names still present.

- [ ] **Step 5: Run the retained focused tests**

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTEST_QT_API = 'pyqt6'
python -m pytest `
    tests/test_main_window.py `
    tests/test_analysis_chart_modules/test_committed_chart_modules.py `
    tests/test_stft_module/test_selection_box_geometry.py `
    -q
```

Expected: all retained tests pass with zero failures.

- [ ] **Step 6: Run the full suite**

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTEST_QT_API = 'pyqt6'
python -m pytest -q
```

Expected: exit code 0, zero failures, and 2,769 passed tests.

If an unrelated intermittent failure occurs, rerun only that node three times and then rerun the full suite once. Do not delete or modify any additional test. If the full suite still does not pass, return `DONE_WITH_CONCERNS` or `BLOCKED` with the exact evidence instead of broadening scope.

- [ ] **Step 7: Verify scope and static cleanliness**

```powershell
git diff --check
git status --short
git diff --name-status
git diff --name-only -- app ui model scripts consts
```

Expected:

- no whitespace errors;
- only the three approved retained test files are modified;
- only `tests/test_seed_historical_normal_report_data.py` is deleted;
- no production paths are listed;
- no manifest is present in the worktree.

Verify the 34 deleted function names do not remain as test definitions anywhere:

```powershell
$contextPath = Join-Path $env:TEMP 'jingcheng-remove-known-failing-tests-context.json'
if (-not (Test-Path -LiteralPath $contextPath)) { throw 'Collection context is missing' }
$context = Get-Content -LiteralPath $contextPath -Raw | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $context.ApprovedFunctionNamesPath)) {
    throw 'Approved function-name manifest is missing'
}
$approvedFunctionNames = @(
    Get-Content -LiteralPath $context.ApprovedFunctionNamesPath
)
foreach ($name in $approvedFunctionNames) {
    $matches = @(
        rg -n --glob '*.py' "^\s*(async\s+)?def\s+$([regex]::Escape($name))\s*\(" tests
    )
    if ($matches.Count -ne 0) {
        throw "Deleted test function still defined: $name`n$($matches -join "`n")"
    }
}
```

- [ ] **Step 8: Commit the deletion**

Stage only the four approved test paths:

```powershell
git add -- `
    tests/test_main_window.py `
    tests/test_seed_historical_normal_report_data.py `
    tests/test_analysis_chart_modules/test_committed_chart_modules.py `
    tests/test_stft_module/test_selection_box_geometry.py
git diff --cached --check
git diff --cached --name-status
git commit -m "test: remove known failing cases"
git status --short
```

Expected: one focused commit containing only the approved test deletions and mechanical same-file cleanup, followed by an empty `git status --short` result.

## Review and Completion Gates

After Task 1 implementation:

1. Dispatch a fresh spec-code reviewer against the plan base through the deletion commit.
2. Dispatch a fresh quality-code reviewer in parallel against the same range.
3. Require both to approve the exact node-ID delta, protected-test preservation, scope, and full-suite result.
4. If either finds a verified issue, return it to the implementer and repeat the dual review.
5. After task-level approval, run a final full-range quality review and invoke `finishing-a-development-branch`.
