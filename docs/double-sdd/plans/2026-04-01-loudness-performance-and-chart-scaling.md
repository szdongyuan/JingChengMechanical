# Loudness Performance And Chart Scaling Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the loudness-driven analysis stall by replacing the current MoSQITo backend path with a much cheaper ISO 532 segmented path, and stop the loudness chart from flattening itself against default `0..60` limits.

**Architecture:** Keep the loudness feature contract intact while narrowing the fix to two boundaries: `compute_loudness_trace()` switches to the segmented MoSQITo backend while preserving app-owned `48 kHz` normalization and loudness failure semantics, and the loudness chart opts into a data-focused Y-range policy in the shared line-chart infrastructure without changing the existing line-chart payload shape.

**Tech Stack:** Python 3.12, PyQt6, pyqtgraph, NumPy, SciPy, pytest, MoSQITo

---

## File Structure

- Modify: `app/service/analysis/dsp.py`
  - Replace the current loudness backend wrapper with the segmented MoSQITo path and keep the existing adapter contract.
- Modify: `app/service/analysis/review_payloads.py`
  - Keep loudness chart payload generation aligned with the unchanged adapter contract.
- Modify: `app/service/analysis/test_manager.py`
  - Revalidate the existing loudness chart-authority warning behavior after the backend swap.
- Modify: `ui/widgets/analysis_charts.py`
  - Add a data-focused Y-range policy for line charts.
- Modify: `ui/modules/loudness.py`
  - Opt the loudness chart into the new Y-range policy.
- Modify: `tests/test_dsp.py`
  - Lock the new backend-selection and non-redundant sample-rate behavior.
- Modify: `tests/test_review_payloads.py`
  - Guard the loudness chart payload contract against backend changes.
- Modify: `tests/test_judgment.py`
  - Revalidate unchanged loudness failure and partial-no-result behavior after the backend swap.
- Modify: `tests/test_test_manager/test_review_payload_emission.py`
  - Revalidate the existing loudness chart warning contract after the backend change.
- Modify: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
  - Add line-chart Y-range mode regressions, including zero-span and clear/reset behavior.
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - Verify the loudness module renders with the expected visible range behavior and keeps existing selection/empty-state behavior.
- Modify: `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
  - Guard the unchanged `series_payload()` shape while the line-chart API grows a new optional keyword.
- Modify: `tests/test_main_window.py`
  - Revalidate the committed loudness chart path through the main window after the chart scaling change.
- Modify: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`
  - Revalidate the factory review path most directly affected by the backend change.

### Task 1: Replace the loudness backend path with the segmented ISO 532 adapter

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`
- Modify: `tests/test_judgment.py`

- [ ] **Step 1: Write the failing loudness adapter tests**

Add focused tests in `tests/test_dsp.py` and `tests/test_judgment.py` for:

```python
def test_compute_loudness_trace_uses_segmented_backend(monkeypatch):
    ...
    assert calls["backend"] == {"sample_rate": 48000.0, "nperseg": 2048, "noverlap": 1024}


def test_compute_loudness_trace_passes_through_native_48k_without_resample(monkeypatch):
    ...
    assert "resample" not in calls
    assert calls["backend"]["sample_rate"] == 48000.0


def test_compute_loudness_trace_normalizes_non_native_sample_rate_before_backend_call(monkeypatch):
    ...
    assert calls["resample"] == {"input_size": 200, "target_size": 100}
    assert calls["backend"]["sample_rate"] == 48000.0


def test_compute_loudness_trace_preserves_times_values_and_max(monkeypatch):
    ...
    assert loudness_times == [0.1, 0.2]
    assert values_sone == [1.5, 2.5]
    assert max_sone == pytest.approx(2.5)


def test_compute_loudness_trace_rejects_non_finite_backend_output(monkeypatch):
    ...
    with pytest.raises(RuntimeError):
        compute_loudness_trace(signal, 48000.0)


def test_compute_loudness_trace_pads_short_input_and_clamps_time_axis(monkeypatch):
    ...
    assert calls["backend"]["input_size"] == 2048
    assert loudness_times == [pytest.approx(actual_duration_sec)]


def test_compute_loudness_trace_normalizes_scalar_one_segment_output(monkeypatch):
    ...
    assert loudness_times == [pytest.approx(0.0213229167)]
    assert values_sone == [pytest.approx(4.0)]
    assert max_sone == pytest.approx(4.0)


def test_compute_loudness_trace_accepts_exact_2048_sample_one_point_output(monkeypatch):
    ...
    assert loudness_times == [pytest.approx(0.0213229167)]
    assert values_sone == [pytest.approx(4.0)]
    assert max_sone == pytest.approx(4.0)


def test_judgment_preserves_loudness_failure_contract_when_segmented_backend_errors(monkeypatch):
    ...
    assert result.channel_results[0].loudness_available is False
    assert result.channel_results[0].loudness_error_message == "backend failed"
```

- [ ] **Step 2: Run the focused adapter tests to confirm failure**

Run: `pytest tests/test_dsp.py tests/test_judgment.py -q`

Expected: FAIL because the current implementation still targets `loudness_zwtv()` and does not implement the segmented backend contract.

- [ ] **Step 3: Implement the segmented loudness adapter**

Update `app/service/analysis/dsp.py` so that:

- `compute_loudness_trace()` loads `mosqito.sq_metrics.loudness_zwst_perseg`
- the backend wrapper passes fixed `nperseg=2048` and `noverlap=1024`
- the current app-owned normalization to `48 kHz` is preserved for non-`48 kHz` inputs
- shorter-than-`2048` normalized inputs are right-padded before the backend call and have returned times clamped to the actual duration
- already-`2048`-sample normalized inputs accept the backend's one-point output as a valid loudness trace
- scalar one-segment backend output is normalized into a one-point loudness trace before shape validation
- the existing return contract remains `times, values_sone, max_sone`
- the same finite-output validation remains in place

- [ ] **Step 4: Re-run the focused adapter tests**

Run: `pytest tests/test_dsp.py tests/test_judgment.py -q`

Expected: PASS

- [ ] **Step 5: Commit the backend change**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py tests/test_judgment.py
git commit -m "fix(analysis): speed up loudness adapter"
```

### Task 2: Preserve loudness payload behavior and add a data-focused loudness Y range

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `ui/widgets/analysis_charts.py`
- Modify: `ui/modules/loudness.py`
- Modify: `tests/test_review_payloads.py`
- Modify: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- Modify: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Modify: `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- Modify: `tests/test_main_window.py`
- Modify: `tests/test_test_manager/test_review_payload_emission.py`
- Modify: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`

- [ ] **Step 1: Write the failing payload and chart-range tests**

Add focused regressions for:

```python
def test_build_chart_payload_keeps_loudness_payload_shape_with_segmented_backend(monkeypatch):
    ...
    assert payload[0]["loudness"] == {
        "times_sec": [0.0, 1.0],
        "values": [2.0, 3.0],
        "unit": "sone",
        "lower_limit": 0.0,
        "upper_limit": 10.0,
    }


def test_build_summary_payload_preserves_loudness_partial_no_result_after_backend_failure_serialization():
    ...
    assert summary["overall_status"] == "no_result"
    assert summary["rows"][1]["status"] == "无结果"
    assert summary["error_message"] == "backend failed"


def test_line_chart_widget_default_auto_mode_preserves_existing_series_payload_shape(qapp):
    ...
    assert widget.series_payload() == {
        "x_values": [0.0, 1.0],
        "y_values": [7.0, 8.0],
        "lower_limit": None,
        "upper_limit": None,
    }


def test_line_chart_widget_auto_mode_keeps_existing_non_finite_acceptance(qapp):
    ...
    widget.set_series([0.0, 1.0], [7.0, float('nan')], y_axis_mode="auto")
    assert math.isnan(widget.series_payload()["y_values"][1])


def test_line_chart_widget_rejects_unknown_y_axis_mode(qapp):
    ...
    with pytest.raises(ValueError):
        widget.set_series([0.0], [1.0], y_axis_mode="nope")


def test_line_chart_widget_data_focus_mode_resets_zero_span_and_clear_state(qapp):
    ...
    assert y_range == pytest.approx((6.3, 7.7))
    widget.clear_data()
    assert widget.plot_item.vb.viewRange()[1] == pytest.approx((0.0, 1.0))


def test_line_chart_widget_data_focus_uses_exact_non_zero_span_padding_formula(qapp):
    ...
    widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="data_focus")
    assert widget.plot_item.vb.viewRange()[1] == pytest.approx((6.9, 8.1))


def test_line_chart_widget_data_focus_rejects_non_finite_values(qapp):
    ...
    previous_y_range = widget.plot_item.vb.viewRange()[1]
    previous_payload = widget.series_payload()
    previous_mode = widget._y_axis_mode
    with pytest.raises(ValueError):
        widget.set_series([0.0, 1.0], [7.0, float('nan')], y_axis_mode="data_focus")
    assert widget._y_axis_mode == previous_mode
    assert widget.series_payload() == previous_payload
    assert widget.plot_item.vb.viewRange()[1] == pytest.approx(previous_y_range)


def test_line_chart_widget_data_focus_rejects_empty_series_without_mutating_prior_state(qapp):
    ...
    previous_y_range = widget.plot_item.vb.viewRange()[1]
    previous_payload = widget.series_payload()
    previous_mode = widget._y_axis_mode
    with pytest.raises(ValueError):
        widget.set_series([], [], y_axis_mode="data_focus")
    assert widget._y_axis_mode == previous_mode
    assert widget.series_payload() == previous_payload
    assert widget.plot_item.vb.viewRange()[1] == pytest.approx(previous_y_range)


def test_line_chart_widget_auto_mode_clears_prior_data_focus_override(qapp):
    ...
    widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="data_focus")
    widget.set_series([0.0, 1.0], [1.0, 9.0], y_axis_mode="auto")
    assert widget.plot_item.vb.state["autoRange"][1] is not False


def test_line_chart_widget_clear_data_in_auto_mode_keeps_backward_compatible_y_behavior(qapp):
    ...
    widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="auto")
    widget.clear_data()
    assert widget.plot_item.vb.state["autoRange"][1] is not False


def test_loudness_module_uses_data_focused_y_range_when_default_limits_are_far_away(qapp):
    ...
    assert module.plot.plot_item.vb.viewRange()[1] == pytest.approx((1.9, 3.1))


def test_test_manager_logs_warning_when_recomputed_loudness_chart_is_missing(monkeypatch):
    ...
    assert any("loudness chart" in call.args[0] for call in log_spy.mock_calls)


def test_test_manager_logs_warning_when_recomputed_loudness_peak_disagrees_with_serialized_scalar(monkeypatch):
    ...
    assert any("scalar mismatch" in call.args[0] and "loudness" in call.args[0] for call in log_spy.mock_calls)
```

- [ ] **Step 2: Run the targeted tests to confirm failure**

Run: `pytest tests/test_review_payloads.py tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py tests/test_main_window.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -q`

Expected: FAIL because the line chart has no data-focused Y-range mode and the loudness module still uses the default auto-range behavior.

- [ ] **Step 3: Implement the payload-safe chart-range change**

Update `ui/widgets/analysis_charts.py` to add a reusable line-chart Y-range mode that:

- computes visible Y bounds from the plotted series
- applies modest padding
- creates a finite fallback span for zero-span data
- resets `data_focus` mode to an explicit empty-state Y range of `0.0 .. 1.0` after `clear_data()`
- accepts only `y_axis_mode="auto"` and `y_axis_mode="data_focus"`
- initializes the widget in stored `y_axis_mode="auto"` and lets a later `y_axis_mode="auto"` call clear any prior data-focused override
- rejects non-finite values only in `y_axis_mode="data_focus"`
- rejects empty `data_focus` series atomically, leaving prior payload, stored mode, and rendered range unchanged
- rejects non-finite `data_focus` series atomically, leaving prior payload, stored mode, and rendered range unchanged
- lets a later `y_axis_mode="auto"` render on the same widget clear any prior `data_focus` override
- keeps `series_payload()` unchanged

Update `ui/modules/loudness.py` so the loudness card opts into that mode when rendering committed series data.

Keep `app/service/analysis/review_payloads.py` changes minimal and only as needed to preserve the existing payload shape under the new backend.

- [ ] **Step 4: Re-run the targeted tests**

Run: `pytest tests/test_review_payloads.py tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py tests/test_main_window.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -q`

Expected: PASS

- [ ] **Step 5: Commit the chart-range change**

```bash
git add app/service/analysis/review_payloads.py app/service/analysis/test_manager.py ui/widgets/analysis_charts.py ui/modules/loudness.py tests/test_review_payloads.py tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py tests/test_main_window.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller/test_reanalysis_and_review.py
git commit -m "fix(ui): scale loudness chart to data"
```

### Task 3: Run the targeted regression suite for the loudness fix slice

**Files:**
- Modify: none expected unless regressions are found
- Test: `tests/test_dsp.py`
- Test: `tests/test_judgment.py`
- Test: `tests/test_review_payloads.py`
- Test: `tests/test_test_manager/test_review_payload_emission.py`
- Test: `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- Test: `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- Test: `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- Test: `tests/test_main_window.py`
- Test: `tests/test_factory_mode_controller/test_reanalysis_and_review.py`

- [ ] **Step 1: Run the targeted regression suite**

Run:

```bash
pytest tests/test_dsp.py tests/test_judgment.py tests/test_review_payloads.py tests/test_test_manager/test_review_payload_emission.py tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py tests/test_main_window.py tests/test_factory_mode_controller/test_reanalysis_and_review.py -q
```

Expected: PASS

- [ ] **Step 2: Run the warm-start loudness timing probes**

Run from the repo root:

```powershell
@'
import json
import time
import numpy as np
from app.service.analysis.dsp import compute_loudness_trace

def run_probe(sample_rate, sample_count):
    buffer = np.random.default_rng(0).standard_normal((sample_count, 2)).astype(np.float64) * 0.01
    compute_loudness_trace(buffer[:, 0], sample_rate)  # warmup
    start = time.perf_counter()
    compute_loudness_trace(buffer[:, 0], sample_rate)
    compute_loudness_trace(buffer[:, 0], sample_rate)
    compute_loudness_trace(buffer[:, 1], sample_rate)
    compute_loudness_trace(buffer[:, 1], sample_rate)
    return time.perf_counter() - start

result = {
    "elapsed_48k_sec": round(run_probe(48000.0, 144000), 4),
    "elapsed_44k1_sec": round(run_probe(44100.0, 132300), 4),
}
print(json.dumps(result))
'@ | python -
```

Expected:

- JSON output includes `elapsed_48k_sec` and `elapsed_44k1_sec`
- `elapsed_48k_sec < 2.0`
- `elapsed_44k1_sec < 2.0`

- [ ] **Step 3: If regressions appear, fix only the touched loudness boundaries**

Keep fixes scoped to:

- `app/service/analysis/dsp.py`
- `app/service/analysis/judgment.py`
- `app/service/analysis/review_payloads.py`
- `app/service/analysis/test_manager.py`
- `ui/widgets/analysis_charts.py`
- `ui/modules/loudness.py`
- the targeted tests above
- `tests/test_factory_mode_controller/test_reanalysis_and_review.py`

- [ ] **Step 4: Commit any final regression fixes if needed**

```bash
git add app/service/analysis/dsp.py app/service/analysis/judgment.py app/service/analysis/review_payloads.py app/service/analysis/test_manager.py ui/widgets/analysis_charts.py ui/modules/loudness.py tests/test_dsp.py tests/test_judgment.py tests/test_review_payloads.py tests/test_test_manager/test_review_payload_emission.py tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py tests/test_main_window.py tests/test_factory_mode_controller/test_reanalysis_and_review.py
git commit -m "fix(loudness): stabilize performance and chart scaling"
```
