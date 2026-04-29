# Live Loudness 250ms Refresh Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change realtime homepage loudness refresh from `0.5s` to `0.25s` while preserving the existing `0.5s` loudness analysis window.

**Architecture:** This is a narrow backend throttle change in `AnalysisPipeline`. Tests should document that loudness emits at 250ms intervals, the rolling window remains half a second, and result-summary SPL behavior is untouched.

**Tech Stack:** Python, NumPy, pytest

---

## Spec

- `docs/double-sdd/specs/2026-04-29-live-loudness-250ms-refresh-design.md`

## File Structure

- Modify `app/service/analysis/pipeline.py`
  - Set `_LIVE_LOUDNESS_THROTTLE_SEC` to `0.25`.
  - Leave `_LIVE_LOUDNESS_WINDOW_SEC` at `0.5`.
- Modify `tests/test_pipeline.py`
  - Update the live loudness cadence test so a frame at `0.25s` emits, a frame before the next 250ms boundary is suppressed, and later 250ms boundaries emit.
  - Update the constants test name/assertion to document `0.25s` throttle and `0.5s` window.
- Modify `docs/double-sdd/specs/2026-04-28-homepage-metrics-performance-refresh-fix-design.md`
  - Update the live loudness throttle reference from `0.5s` to `0.25s`.
- Modify `docs/double-sdd/plans/2026-04-28-homepage-metrics-performance-refresh-fix.md`
  - Update the live loudness throttle reference from `0.5s` to `0.25s`.

## Task 1: Update Live Loudness Throttle

**Files:**
- Modify: `app/service/analysis/pipeline.py`
- Modify: `tests/test_pipeline.py`
- Modify: `docs/double-sdd/specs/2026-04-28-homepage-metrics-performance-refresh-fix-design.md`
- Modify: `docs/double-sdd/plans/2026-04-28-homepage-metrics-performance-refresh-fix.md`

- [ ] **Step 1: Update tests first**

In `tests/test_pipeline.py`, adjust `test_process_chunk_returns_throttled_loudness_frame` to use elapsed times around the new throttle:

```python
first = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.25)
second = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.40)
third = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.50)
fourth = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.75)
```

Assert:

- `first["loudness"]` is a `LoudnessFrame` at `0.25`.
- `second["loudness"] is None`.
- `third["loudness"]` is a `LoudnessFrame` at `0.50`.
- `fourth["loudness"]` is a `LoudnessFrame` at `0.75`.
- `len(calls) == 3`.

Rename `test_live_loudness_throttle_and_window_are_half_second` to describe the new values, and assert:

```python
assert _LIVE_LOUDNESS_THROTTLE_SEC == pytest.approx(0.25)
assert _LIVE_LOUDNESS_WINDOW_SEC == pytest.approx(0.5)
```

- [ ] **Step 2: Run the focused pipeline test and confirm it fails before implementation**

Run:

```powershell
pytest tests/test_pipeline.py::TestAnalysisPipeline::test_process_chunk_returns_throttled_loudness_frame tests/test_pipeline.py::TestAnalysisPipeline::test_live_loudness_throttle_is_quarter_second_and_window_is_half_second -q
```

Expected before implementation: FAIL because `_LIVE_LOUDNESS_THROTTLE_SEC` is still `0.5`.

- [ ] **Step 3: Implement the throttle change**

In `app/service/analysis/pipeline.py`, change:

```python
_LIVE_LOUDNESS_THROTTLE_SEC = 0.5
```

to:

```python
_LIVE_LOUDNESS_THROTTLE_SEC = 0.25
```

Do not change `_LIVE_LOUDNESS_WINDOW_SEC`.

- [ ] **Step 4: Update matching docs**

Update the existing homepage metrics performance spec and plan references so they no longer state that the live loudness throttle is `0.5s`. The window remains `0.5s`.

- [ ] **Step 5: Run verification**

Run:

```powershell
pytest tests/test_pipeline.py -q
pytest tests/test_result_module.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add app/service/analysis/pipeline.py tests/test_pipeline.py docs/double-sdd/specs/2026-04-29-live-loudness-250ms-refresh-design.md docs/double-sdd/plans/2026-04-29-live-loudness-250ms-refresh.md docs/double-sdd/specs/2026-04-28-homepage-metrics-performance-refresh-fix-design.md docs/double-sdd/plans/2026-04-28-homepage-metrics-performance-refresh-fix.md
git commit -m "fix(result): refresh live loudness every 250ms"
```

Leave merging back to the orchestrator.
