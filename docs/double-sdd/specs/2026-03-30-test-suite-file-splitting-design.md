# Test Suite File Splitting Design

## Context

Several test modules in `tests/` have grown beyond 1000 lines:

- `tests/test_factory_mode_controller.py`
- `tests/test_test_manager.py`
- `tests/test_analysis_chart_modules.py`
- `tests/test_stft_module.py`

These files now mix multiple behaviors, helper scaffolding, and regression areas in a single module. That makes them harder to navigate, harder to extend safely, and slower to reason about during future debugging work.

The user wants the oversized files split, preferably by functionality, and is fine with placing the resulting modules into folders.

## Scope

In scope:

- split these four current oversized source test files:
  - `tests/test_factory_mode_controller.py`
  - `tests/test_test_manager.py`
  - `tests/test_analysis_chart_modules.py`
  - `tests/test_stft_module.py`
- organize each split around functional groupings that already exist in the test names and helper usage
- move shared helpers for each split area into local support modules when that keeps the individual test files smaller and clearer
- keep the tests discoverable and runnable by pytest from the repository root
- keep the actual tested behavior unchanged
- update test imports or helper locations as needed to support the new structure

Out of scope:

- changing production code behavior
- rewriting unrelated smaller test files
- cleaning up historical docs or old plan references that mention the previous long-file paths
- introducing new test behavior beyond what is required to preserve the existing suite

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - pytest discovery from the repository root
  - the set of behaviors covered by the split tests
  - local helper semantics relied on by those tests
- Allowed breakage:
  - direct file paths and node ids for the four oversized test modules
  - historical docs that reference the old single-file paths
- Migration strategy: none

This task protects the suite's executable behavior, not the old file layout.

## Approaches Considered

### Approach A: Extract helper modules only

Keep the long files in place and move only fixtures/helpers out.

Pros:

- smallest path churn
- lowest immediate risk

Cons:

- does not satisfy the main goal of splitting oversized test files
- leaves the largest navigation problem in place

### Approach B: Replace each oversized test file with a same-topic folder split by functionality

Create a dedicated folder per oversized test area and divide the tests into focused modules inside it.

Pros:

- directly solves the oversized-file problem
- matches the user's preference for functionality-based grouping
- keeps related helpers close to the tests that use them

Cons:

- changes some pytest file paths and node ids
- requires modest import/helper cleanup

### Approach C: Split by arbitrary size buckets

Break each long file into `part1`, `part2`, and so on.

Pros:

- quick mechanical split

Cons:

- poor long-term navigability
- weak semantic boundaries
- future contributors still need to search across arbitrary buckets

## Recommended Design

Use Approach B.

Each oversized test area should become its own folder under `tests/`, named after the original topic, with focused test modules grouped by behavior. The split should follow the existing structure that is already visible in class names, helper families, and test-name prefixes, rather than enforcing uniform file sizes.

## Structural Rules

Each split area must use the same folder pattern:

- create a folder at the original topic path, for example `tests/test_stft_module/`
- make that folder a Python package with `__init__.py`
- move true shared pytest fixtures for that folder into a local `conftest.py`
- move non-fixture helpers, stubs, builders, and fake objects into non-collectable support modules
- keep helper filenames non-collectable by pytest; only `conftest.py` and `test_*.py` may be collected
- do not share helper modules across unrelated split areas
- do not keep a compatibility wrapper at the original `tests/test_*.py` file path; the old oversized file is removed once the folderized replacement exists

This makes fixture scope explicit, keeps helper imports predictable, and avoids accidental pytest collection of support code.

Any test that currently reaches repo files with `Path(__file__)` must be updated for its new nesting depth as part of the split. The split is not allowed to leave file-relative repo lookups broken.

Support-module rule:

- start with one local `_support.py` per split area only if that file stays focused and comfortably below the oversized-file threshold
- if support code would exceed 1000 lines or mixes unrelated helper concerns, split it into multiple non-collectable modules such as `_support_builders.py`, `_support_fakes.py`, or `_support_assertions.py`
- no support module may exceed 1000 lines

## Functional Grouping Plan

### `test_factory_mode_controller`

Replace `tests/test_factory_mode_controller.py` with:

- `tests/test_factory_mode_controller/__init__.py`
- `tests/test_factory_mode_controller/conftest.py` only if multiple split modules need shared pytest fixtures; otherwise omit it
- `tests/test_factory_mode_controller/_support.py` and additional `_support_*.py` modules only if needed to stay below 1000 lines
- `tests/test_factory_mode_controller/test_readiness_and_selection.py`
- `tests/test_factory_mode_controller/test_trigger_routing.py`
- `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- `tests/test_factory_mode_controller/test_reanalysis_and_review.py`
- `tests/test_factory_mode_controller/test_cleanup_and_abort.py`

Module responsibilities:

- `test_readiness_and_selection.py`: startup readiness, initial selection rules, validation gating, missing/deleted selection persistence behavior
- `test_trigger_routing.py`: PLC and scan routing, blocked trigger logging, trigger behavior while armed or idle
- `test_pending_start_lifecycle.py`: manual/public start acceptance, stale callback rejection, pending-start cancellation, start exceptions and blocked-start feedback
- `test_stop_and_active_run.py`: active-phase start blocking, selection-change blocking, stop success/failure/exception behavior, active-run ownership preservation
- `test_reanalysis_and_review.py`: review payload acceptance, reanalysis start/reset behavior, sparse payload validation, manual review reseeding
- `test_cleanup_and_abort.py`: cleanup-settled readiness recomputation, auto-analysis terminal transitions, runtime abort behavior, real-manager lifecycle bridge tests

Local scaffolding such as bus stubs, payload builders, and service factories must live in non-collectable support modules unless a helper is private to one split file.

### `test_test_manager`

Replace `tests/test_test_manager.py` with:

- `tests/test_test_manager/__init__.py`
- `tests/test_test_manager/conftest.py` only if multiple split modules need shared pytest fixtures; otherwise omit it
- `tests/test_test_manager/_support.py` and additional `_support_*.py` modules only if needed to stay below 1000 lines
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

Responsibilities:

- `test_init.py`: constructor and static helper typing checks
- `test_start_inputs_and_resolution.py`: config-id/name resolution, device identity wiring, monitor-channel selection, config-load behavior
- `test_start_acceptance_and_failures.py`: acceptance ordering, request cancellation, start-time failures, recorder/monitor error handling, fast-finish edge cases
- `test_stop_behavior.py`: stop-all, stop-by-config, partial-save cleanup, missing-run stop behavior
- `test_rejudge.py`: all cases currently under `TestTestManagerRejudge`
- `test_shared_recorder.py`: all cases currently under `TestSharedRecorderTeardown`
- `test_sample_rate_mismatch.py`: all cases currently under `TestSampleRateMismatch`
- `test_max_duration.py`: all cases currently under `TestSharedRecorderMaxDuration`
- `test_late_attach_buffer_slice.py`: all cases currently under `TestLateAttachBufferSlice`
- `test_user_stop_partial_save.py`: all cases currently under `TestUserStopPartialSave`
- `test_signal_disconnection.py`: all cases currently under `TestSignalDisconnection`
- `test_review_payload_emission.py`: all cases currently under `TestReviewPayloadEmission`

This split follows existing scenario boundaries, but `TestTestManagerStartStop` must be subdivided as above so every resulting test file stays below the 1000-line threshold. Common fake recorder/session/bus helpers must live in non-collectable support modules.

### `test_analysis_chart_modules`

Replace `tests/test_analysis_chart_modules.py` with:

- `tests/test_analysis_chart_modules/__init__.py`
- `tests/test_analysis_chart_modules/conftest.py`
- `tests/test_analysis_chart_modules/_support.py` and additional `_support_*.py` modules only if needed to stay below 1000 lines
- `tests/test_analysis_chart_modules/test_runtime_dependency.py`
- `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`

Responsibilities:

- `conftest.py`: shared `qapp` fixture for the folder
- support modules: signal/bus stubs, state builders, chart payload builders, geometry assertions
- `test_runtime_dependency.py`: the `requirements.txt` dependency test, with its file-relative repo-root lookup updated for the folder nesting depth
- `test_widget_payload_and_geometry.py`: direct chart widget payload, geometry, transform, and marker/error-bar behavior
- `test_zero_based_axes.py`: zero-based axis and empty-state axis policy tests
- `test_committed_chart_modules.py`: SPL/spectrum/octave committed-render and no-data-state behavior

Shared Qt app fixture helpers and chart payload builders must stay centralized for that area.

### `test_stft_module`

Replace `tests/test_stft_module.py` with:

- `tests/test_stft_module/__init__.py`
- `tests/test_stft_module/conftest.py`
- `tests/test_stft_module/_support.py` and additional `_support_*.py` modules only if needed to stay below 1000 lines
- `tests/test_stft_module/test_live_review_rendering.py`
- `tests/test_stft_module/test_review_window_controls.py`
- `tests/test_stft_module/test_selection_box_geometry.py`
- `tests/test_stft_module/test_reanalysis_and_reset.py`

Responsibilities:

- `conftest.py`: shared `qapp` fixture for the folder
- support modules: bus stubs, module/service builders, review payload builders, STFT-history seeding helpers, box assertions
- `test_live_review_rendering.py`: live-frame persistence and selected-channel rendering across running/analyzing/reviewing
- `test_review_window_controls.py`: review-success/default-window display, fallback/manual-window behavior, analysis-control visibility
- `test_selection_box_geometry.py`: plotted-edge alignment, box bounds, movement/clamping, drag geometry, geometry-unavailable no-op behavior
- `test_reanalysis_and_reset.py`: reanalysis unlock/lock flow, controller reseeding after reanalysis, display reset behavior

The shared bus/module construction helpers must stay local to that folder.

## File-Layout Rules

- remove the original oversized single-file modules after the split
- keep the new test modules below the 1000-line threshold
- keep every support module below the 1000-line threshold
- prefer one small support module per split folder, but split support code by concern once it would otherwise become oversized
- keep filenames descriptive enough that a contributor can infer the covered behavior from the path alone
- do not create a shared mega-helper module across unrelated test areas

## Testing Requirements

Verification must prove:

- a pre-change targeted node inventory is captured with:
  - `pytest tests/test_factory_mode_controller.py tests/test_test_manager.py tests/test_analysis_chart_modules.py tests/test_stft_module.py --collect-only -q`
- a pre-change baseline is captured with:
  - `pytest tests -q`
- the split areas are discoverable with:
  - `pytest tests/test_factory_mode_controller tests/test_test_manager tests/test_analysis_chart_modules tests/test_stft_module --collect-only -q`
- the split areas pass together with:
  - `pytest tests/test_factory_mode_controller tests/test_test_manager tests/test_analysis_chart_modules tests/test_stft_module -q`
- the full suite is rerun with:
  - `pytest tests -q`

Acceptance rules:

- after normalizing away the file-path prefix, the pre-change and post-change targeted collect inventories must contain the same set of test function names and parametrization ids
- the collect-only command must succeed with no new collection errors
- the four split areas must pass in the targeted run
- the post-change full-suite run must introduce no new failures relative to the pre-change baseline
- any baseline failures outside the four split areas are non-blocking only if they reproduce unchanged after the split

## Non-Goals

This task does not:

- preserve the previous single-file node ids
- optimize runtime of the tests
- change assertions or coverage scope unless a split requires tiny import-path adjustments
