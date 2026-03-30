# Main Window STFT Review Test Repair Design

## Context

After the oversized-test split, the full suite still reproduces two pre-existing failures in `tests/test_main_window.py`:

- `test_next_run_reset_clears_result_stft_and_chart_cards_before_next_review_payload_renders`
- `test_selection_changed_reset_clears_result_stft_and_chart_cards`

Both fail because they expect `window.stft.selection_box.isVisible()` to be `True` immediately after `_emit_review_ready(...)`, but the helper never emits any STFT frames.

Current STFT behavior is already covered elsewhere and is consistent:

- the STFT selection box is only renderable when the module has review selection data and rendered STFT payload geometry
- when rendered STFT payload is unavailable, the box stays hidden

That behavior is exercised by the dedicated STFT tests and should remain the contract.

## Scope

In scope:

- repair the failing `tests/test_main_window.py` review/reset scenarios so they provide STFT render data before asserting selection-box visibility
- keep the current STFT module behavior unchanged
- keep the main-window reset assertions intact once realistic STFT review data has been seeded
- add or adjust only the test helpers/assertions needed to reflect the existing STFT contract

Out of scope:

- changing production behavior in `ui/modules/stft.py`
- changing the STFT geometry or review-window contract
- weakening the failing assertions so they stop checking selection-box visibility

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces:
  - current `StftModule` rule that the selection box hides when rendered STFT payload is unavailable
  - current main-window reset behavior for result and chart cards
  - current targeted split-test package layout created on this branch
- Allowed breakage:
  - outdated main-window test helpers that treated review metadata alone as sufficient for STFT box visibility
- Migration strategy: none

## Approaches Considered

### Approach A: Change `StftModule` to show the box without STFT payload

Pros:

- makes the two failing tests pass without touching them

Cons:

- contradicts the current STFT tests and geometry contract
- risks showing a selection box without render geometry

### Approach B: Repair the main-window tests/helpers to seed STFT data before review-ready assertions

Pros:

- matches the current STFT contract
- keeps production behavior unchanged
- fixes the real mismatch in the failing tests

Cons:

- requires touching shared main-window test helpers

### Approach C: Remove the selection-box visibility assertions

Pros:

- smallest patch

Cons:

- weakens coverage
- does not model the real review-ready state correctly

## Recommended Design

Use Approach B.

The main-window tests should treat “review ready” as a state where both committed chart payloads and STFT render data are available. Add a small STFT test helper that emits minimal STFT frames for the requested run and channels, then call it from the shared `_emit_review_ready(...)` helper in `tests/test_main_window.py` before that helper emits review state and review payload events.

## Design Details

### Test Helper Contract

`tests/test_main_window.py` should gain a helper that emits a minimal valid STFT history for a given `config_id`, `run_id`, and channel set.

Requirements:

- emit STFT frames for every channel in the helper's `channels` argument
- emit a concrete minimal timeline that fully covers the current default review window of `1.0 -> 3.0` seconds
- the helper should seed frames at `0.0`, `1.0`, `2.0`, and `3.0` seconds for each emitted channel so `StftModule` can build a rendered payload and overlay geometry deterministically while keeping the helper simple
- use the same `run_id` that the review payload will reference
- default to the same channel tuple already passed to the committed-chart review helper so the first display channel and any toolbar-selected review channel both have cached STFT data available

### Review-Ready Simulation

The failing review/reset scenarios should reach review state through a realistic sequence:

1. runtime start accepted
2. STFT frames for the active run are emitted
3. review state + review payload are emitted

This sequence should be applied through the shared helper path rather than duplicating bespoke STFT setup in each failing test.

### Assertion Policy

Keep the existing assertions:

- before reset, the selection box should be visible once review-ready STFT data exists
- after `factory_display_reset`, the selection box should be hidden again
- committed chart cards should continue following the current reset and rerender expectations

## Testing Requirements

Verification must prove:

- `pytest tests/test_main_window.py -k "next_run_reset_clears_result_stft_and_chart_cards_before_next_review_payload_renders or selection_changed_reset_clears_result_stft_and_chart_cards" -q` passes
- `pytest tests/test_main_window.py -q` introduces no new failures in that file
- `pytest tests -q` passes

## Non-Goals

This task does not:

- revisit the split-test packaging work
- change the STFT module API
- alter unrelated `tests/test_main_window.py` scenarios
