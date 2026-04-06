# SPL Boundary Tail Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the committed SPL review chart from emitting an invalid boundary point when the selected analysis window ends with less than one full 50 ms SPL chart chunk.

**Architecture:** Keep the existing committed SPL chart contract and fixed 50 ms chart cadence, but change the payload builder so it only emits SPL points for complete chunks. Add focused regression coverage at the payload layer and one committed SPL module behavior check for the empty-series case.

**Tech Stack:** Python 3.12, NumPy, PyQt6, pyqtgraph, pytest

---

## File Structure

- Modify: `app/service/analysis/review_payloads.py`
  - Keep `_SPL_CHUNK_DURATION_SEC` and `spl_time_constant` semantics unchanged.
  - Update `_build_spl_chart(...)` so trailing partial chunks do not produce committed SPL points.
- Modify: `tests/test_review_payloads.py`
  - Add focused regression tests for:
    - trailing partial tail omission
    - exact full-chunk boundary retention
    - shorter-than-one-chunk empty SPL payload behavior
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - Add one committed SPL module regression verifying that an empty committed SPL payload keeps the chart blank/no-data.

### Task 1: Fix Committed SPL Boundary-Tail Emission

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Test: `tests/test_review_payloads.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`

- [ ] **Step 1: Write the failing payload regressions**

Add focused tests in `tests/test_review_payloads.py` that lock down the new chunk-boundary contract:

```python
def test_build_chart_payload_spl_omits_trailing_partial_tail_for_a_weighted_series():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples * 2 + 1, 0.1, dtype=np.float32).reshape(-1, 1)

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"][0]["spl_weighting"] = "A"

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [0.05, 0.1]
    assert len(payload[0]["spl"]["values_db"]) == 2


def test_build_chart_payload_spl_omits_trailing_partial_tail_for_c_weighted_series():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples * 2 + 1, 0.1, dtype=np.float32).reshape(-1, 1)

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"][0]["spl_weighting"] = "C"

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [0.05, 0.1]
    assert len(payload[0]["spl"]["values_db"]) == 2


def test_build_chart_payload_spl_exact_chunk_boundary_keeps_full_point_count():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples * 2, 0.1, dtype=np.float32).reshape(-1, 1)

    payload = build_chart_payload(
        buffer=signal,
        config=_make_config(),
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [0.05, 0.1]
    assert len(payload[0]["spl"]["values_db"]) == 2


def test_build_chart_payload_spl_shorter_than_one_chunk_returns_empty_series():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples - 1, 0.1, dtype=np.float32).reshape(-1, 1)

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"][0]["spl_weighting"] = "C"

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"] == {
        "times_sec": [],
        "values_db": [],
        "lower_limit_db": 40.0,
        "upper_limit_db": 80.0,
    }
```

- [ ] **Step 2: Write the failing committed-module regression**

Add a focused `SplModule` compatibility regression in `tests/test_analysis_chart_modules/test_committed_chart_modules.py` that reuses the existing empty-chart helper:

```python
def test_spl_module_keeps_chart_blank_when_committed_payload_has_no_series_points(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SplModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["spl"] = {
        "times_sec": [],
        "values_db": [],
        "lower_limit_db": 40.0,
        "upper_limit_db": 80.0,
    }

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)
```

- [ ] **Step 3: Run the payload regressions to verify they fail**

Run:

```powershell
pytest tests/test_review_payloads.py -q -k "spl_omits_trailing_partial_tail_for_a_weighted_series or spl_omits_trailing_partial_tail_for_c_weighted_series or spl_exact_chunk_boundary or spl_shorter_than_one_chunk"
```

Expected: FAIL because `_build_spl_chart(...)` currently emits a final partial chunk and the new payload regressions are not yet implemented.

- [ ] **Step 4: Run the committed-module compatibility regression before the code change**

Run:

```powershell
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "spl_module_keeps_chart_blank_when_committed_payload_has_no_series_points"
```

Expected: PASS because the committed `SplModule` should already keep an empty-series committed payload in the existing blank/no-data state.

- [ ] **Step 5: Implement the minimal committed SPL chart fix**

Update `app/service/analysis/review_payloads.py` so `_build_spl_chart(...)` emits points only for complete 50 ms chunks.

Implementation notes:

- keep `_SPL_CHUNK_DURATION_SEC = 0.05`
- keep `dsp.compute_spl(...)` call semantics unchanged
- do not pad or synthesize tail samples
- do not alter lower/upper limit serialization
- the simplest acceptable implementation is to skip any chunk whose length is smaller than `chunk_samples`, or iterate only across complete chunk starts

- [ ] **Step 6: Run the focused regressions**

Run:

```powershell
pytest tests/test_review_payloads.py -q -k "spl_omits_trailing_partial_tail_for_a_weighted_series or spl_omits_trailing_partial_tail_for_c_weighted_series or spl_exact_chunk_boundary or spl_shorter_than_one_chunk"
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "spl_module_keeps_chart_blank_when_committed_payload_has_no_series_points or test_spl_module_renders_committed_chart_for_selected_channel"
```

Expected: PASS

- [ ] **Step 7: Run the broader directly affected chart and payload coverage**

Run:

```powershell
pytest tests/test_review_payloads.py -q -k "build_chart_payload"
pytest tests/test_analysis_chart_modules/test_committed_chart_modules.py -q -k "spl_module"
```

Expected: PASS

- [ ] **Step 8: Commit the committed SPL chart fix**

```bash
git add app/service/analysis/review_payloads.py tests/test_review_payloads.py tests/test_analysis_chart_modules/test_committed_chart_modules.py
git commit -m "fix(spl): omit invalid committed boundary tail point"
```
