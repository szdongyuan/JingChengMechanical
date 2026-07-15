# Remove Known Failing Tests Design

## Status

Approved design. Awaiting written-spec review and final user confirmation before planning.

## Context

The `codex/session-capture-warmup-20260714` branch has 35 consistently identified failing pytest cases outside the 50 ms session-warmup implementation. The user explicitly chose deletion rather than repairing, skipping, or marking these cases as expected failures.

The 35 collected cases correspond to 34 Python test functions because one MainWindow test is parameterized for two roles.

## Goals

- Remove exactly the 35 identified failing pytest cases.
- Preserve every currently passing test in the touched files.
- Remove test-only imports and helper definitions that become unreferenced solely because of these deletions.
- Leave production code, runtime behavior, dependencies, configuration, database behavior, and the 50 ms session-warmup implementation unchanged.
- Finish with a clean full-suite pytest run.

## Non-goals

- Do not repair the MainWindow test double.
- Do not change the seed script or database fixture.
- Do not change chart-axis visibility behavior.
- Do not change STFT selection-box geometry behavior.
- Do not replace deleted tests with `skip`, `xfail`, aliases, renamed equivalents, or new tests covering the same assertions.
- Do not delete passing tests merely because they share a file or helper with a failing test.
- Do not refactor production or test architecture.

## Chosen Approach

Delete the exact failing test functions. Delete a whole test file only when every collected test in that file is in the approved deletion set. After deletion, perform narrowly mechanical cleanup of imports and test-only helpers that have no remaining references in the same file.

This is preferred over deleting whole mixed-result files, which would remove passing coverage, and over `skip`/`xfail`, which would retain rather than delete the tests.

## Exact Deletion Set

### MainWindow: 25 collected cases from 24 functions

Modify `tests/test_main_window.py` and delete these functions:

1. `test_show_change_password_window_when_logged_out_updates_visible_status_via_log`
2. `test_main_window_includes_ai_tool_management_tab_in_topbar`
3. `test_engineer_role_can_access_ai_tool_management_tab`
4. `test_admin_role_can_access_ai_tool_management_tab`
5. `test_operator_role_cannot_access_ai_tool_management_tab`
6. `test_no_role_programmatic_page_reentry_remains_allowed`
7. `test_operator_cannot_activate_ai_tool_management_via_programmatic_tab_change`
8. `test_switching_away_from_ai_tool_management_is_blocked_when_confirm_leave_rejects`
9. `test_programmatic_tab_change_reverts_when_ai_tool_management_rejects_leave`
10. `test_switching_away_from_config_is_blocked_when_confirm_leave_rejects`
11. `test_switching_away_from_config_is_allowed_when_confirm_leave_accepts`
12. `test_role_switch_from_admin_to_operator_stays_consistent_when_ai_leave_is_rejected`
13. `test_closing_main_window_is_blocked_when_ai_tool_management_rejects_leave`
14. `test_closing_main_window_is_blocked_when_config_rejects_leave`
15. `test_admin_and_engineer_roles_can_use_plc_settings_card`
16. `test_closing_main_window_stops_plc_cleanup_before_shared_shutdown_and_camera`
17. `test_closing_main_window_uses_shared_shutdown_boundary_and_stops_camera`
18. `test_closing_main_window_still_uses_shared_shutdown_and_camera_when_plc_cleanup_raises`
19. `test_closing_main_window_swallows_camera_cleanup_errors_after_shared_shutdown`
20. `test_closing_main_window_still_stops_camera_when_shared_shutdown_raises`
21. `test_main_window_uses_result_dashboard_module_for_right_side`
22. `test_vibration_panel_titles_use_parentheses`
23. `test_main_window_keeps_fixed_left_column_modules_unchanged`
24. `test_main_window_routes_latest_log_line_to_status_bar`

`test_admin_and_engineer_roles_can_use_plc_settings_card` currently collects two cases, one for `管理员` and one for `工程师`. Removing the function therefore removes two of the 25 collected cases.

The following five passing tests in `tests/test_main_window.py` are protected and must remain collected:

- `test_card_compact_dashboard_mode_reduces_layout_chrome`
- `test_card_title_suffix_does_not_overlap_title_at_narrow_width`
- `test_card_title_metric_uses_score_value_box_style`
- `test_dashboard_compact_card_qss_removes_card_chrome`
- `test_card_empty_title_suffix_is_hidden_and_title_uses_row_width`

### Historical seed data: 3 cases

Delete `tests/test_seed_historical_normal_report_data.py` because all three functions in the file are approved for deletion:

1. `test_seed_mock_normal_report_data_creates_reusable_normal_distribution_rows`
2. `test_seed_mock_normal_report_data_is_idempotent_for_prefix`
3. `test_seed_mock_normal_report_data_can_clear_all_report_rows`

### Analysis chart axis labels: 2 cases

Modify `tests/test_analysis_chart_modules/test_committed_chart_modules.py` and delete:

1. `test_spectrum_module_keeps_chinese_axis_labels_visible`
2. `test_stft_module_keeps_chinese_axis_labels_visible`

All other tests in this file must remain unchanged except for mechanical cleanup of newly unused test-only imports or helpers.

### STFT selection-box geometry: 5 cases

Modify `tests/test_stft_module/test_selection_box_geometry.py` and delete:

1. `test_selection_box_height_matches_plot_area_and_does_not_extend_below_it`
2. `test_single_sample_stft_selection_box_uses_one_frame_tolerance_formula`
3. `test_move_selection_to_start_clamps_to_captured_plot_intersection`
4. `test_preseeded_selection_box_render_clamps_to_latest_valid_start_inside_captured_plot_intersection`
5. `test_dragging_passive_selection_box_does_not_move_selected_window`

The other 19 currently passing tests in this file must remain collected.

## Cleanup Rules

- Cleanup is limited to the three retained test files named above plus deletion of `tests/test_seed_historical_normal_report_data.py`.
- A test-only import or helper may be removed only when it has no remaining reference after the approved test deletions.
- Do not rename, rewrite, consolidate, or otherwise alter the behavior of retained tests.
- Do not modify shared fixtures or support files, including `tests/conftest.py` and `tests/test_stft_module/_support.py`, even if a symbol there becomes unreferenced.
- Do not touch any path under `app/`, `ui/`, `model/`, `scripts/`, `consts/`, or dependency files.

## Verification

1. Deterministic collection comparison:
   - Before deleting tests, collect all pytest node IDs in the implementation worktree with the same Python executable, environment variables, pytest options, and working directory that will be used after deletion. Store the baseline manifest outside the repository.
   - After deletion, collect node IDs again under the identical environment and normalize both manifests in the same way.
   - The set difference `baseline - post-deletion` must contain exactly the approved 35 collected cases: one node ID for every named function except `test_admin_and_engineer_roles_can_use_plc_settings_card`, which contributes the two approved `管理员` and `工程师` parameterized node IDs.
   - The reverse set difference `post-deletion - baseline` must be empty.
   - Any additional removed or added node ID is a verification failure and must not be explained away as an environment-dependent count difference.
2. File-level collection checks:
   - `tests/test_main_window.py` collects exactly 5 tests, matching the protected list.
   - `tests/test_stft_module/test_selection_box_geometry.py` collects exactly 19 tests.
   - The two named chart-axis tests are absent.
   - `tests/test_seed_historical_normal_report_data.py` no longer exists.
   - None of the 34 deleted function names appear anywhere in the repository as collected replacement tests.
3. Focused execution:
   - The retained MainWindow tests pass.
   - The retained chart-module tests pass.
   - The retained STFT geometry tests pass.
4. Full execution:
   - `python -m pytest -q` exits successfully with zero failures.
   - A total of 2,769 collected tests is expected from the observed 2,804-case baseline, but the exact node-ID set comparison is the authoritative preservation check.
5. Static scope:
   - `git diff --check` passes.
   - The implementation diff contains no production-code changes.
   - The worktree is clean after the deletion commit.

If an unrelated intermittent test fails during verification, isolate and report it. Do not delete any additional test without a new explicit user decision.

## Compatibility / Migration

- **Backward compatibility:** required for all production and user-visible behavior; intentionally not required for the approved test cases.
- **Protected surfaces:** production APIs, UI behavior, configuration, database schema and data, file formats, imported and golden-sample workflows, session warmup behavior, dependencies, and all retained tests.
- **Allowed breakage:** removal of exactly 35 collected pytest cases from the approved deletion set and removal of test-only code made unreachable by those deletions.
- **Migration strategy:** none.

## Risks and Controls

- **Risk: excess coverage removal.** Control: exact named deletion list plus retained-test collection counts.
- **Risk: hiding new failures by broad deletion.** Control: no additional deletions and a full-suite run.
- **Risk: accidental production changes.** Control: production paths are explicitly forbidden and verified with a diff scope check.
- **Risk: dead test scaffolding.** Control: allow only mechanically proven, test-local unused-code cleanup.
