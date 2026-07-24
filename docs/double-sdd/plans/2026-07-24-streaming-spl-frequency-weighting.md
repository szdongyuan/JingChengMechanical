# Streaming SPL Frequency Weighting Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-block FFT SPL weighting with one continuous stateful A/C filter used by real-time, post-run chart, and judgment paths while preserving Z weighting and existing external contracts.

**Architecture:** Add a focused `spl_weighting` module that owns analytic reference curves, sample-rate-specific SOS coefficient design, and per-stream filter state. Keep `dsp.SplDetector` as the streaming facade, extend SPL trace/Leq helpers with explicit full-capture output bounds, and make judgment and chart callers process from capture start before selecting the analysis window.

**Tech Stack:** Python 3, NumPy, SciPy `signal`, pytest, existing analysis pipeline and payload services.

---

## Specification

Authoritative design:
`docs/double-sdd/specs/2026-07-24-streaming-spl-frequency-weighting-design.md`

The implementation must preserve the approved compatibility posture and meet
the frequency-response and chunk-invariance tolerances in that spec.

## File Structure

- Create `app/service/analysis/spl_weighting.py`
  - Own IEC A/C constants and analytic reference curves.
  - Design and cache immutable SOS coefficients.
  - Own `SplFrequencyWeightingFilter` and its per-instance delay state.
- Modify `app/service/analysis/dsp.py`
  - Re-export `a_weighting` and `c_weighting`.
  - Compose the stateful weighting filter into `SplDetector`.
  - Make trace and Leq helpers support continuous full-capture processing with
    bounded output/statistics windows.
- Modify `app/service/analysis/judgment.py`
  - Compute the SPL trace once from calibrated full-capture samples.
  - Select only analysis-window trace points for final/min/max/limit results.
  - Compute Leq from continuously weighted samples but window-only energy.
- Modify `app/service/analysis/review_payloads.py`
  - Pass calibrated full-capture samples and sample bounds to SPL chart
    construction.
  - Serialize absolute trace times without restarting weighting at the window.
- Create `tests/test_spl_weighting.py`
  - Focused coefficient, response, state, reset, fallback, and independence
    coverage.
- Modify `tests/test_dsp.py`
  - Cover detector integration, bounded traces, final partial chunks, Leq
    bounds, and Z compatibility.
- Modify `tests/test_pipeline.py`
  - Cover low-frequency live regression, chunk continuity, and reset.
- Modify `tests/test_test_session.py`
  - Cover independent weighting state for distinct live session pipelines.
- Modify `tests/test_judgment.py`
  - Cover full-capture pre-roll, shared trace authority, Leq bounds, and limits.
- Modify `tests/test_review_payloads.py`
  - Cover full-capture chart pre-roll, absolute times, final partial points, and
    corrected 31.5 Hz values.
- Modify `tests/test_imported_audio_analysis.py`
  - Cover fresh weighting state across repeated imported-file analyses.
- Modify `tests/test_live_display_dispatcher.py` only if focused integration
  coverage shows the existing gap-reset test cannot prove the complete SPL
  detector state is reset.

## Task 1: Stateful A/C SOS Weighting Component

**Files:**
- Create: `app/service/analysis/spl_weighting.py`
- Create: `tests/test_spl_weighting.py`
- Modify: `app/service/analysis/dsp.py` only to re-export moved analytic
  functions if required by the new tests

- [ ] **Step 1: Write failing analytic-response and coefficient-cache tests**

Create parameterized tests over `A` and `C`, sample rates 44,100, 48,000, and
96,000 Hz, and the approved standard center frequencies.

```python
@pytest.mark.parametrize(
    ("sample_rate", "max_frequency_hz", "tolerance_db"),
    [
        (44_100.0, 12_500.0, 1.0),
        (48_000.0, 12_500.0, 1.0),
        (96_000.0, 20_000.0, 1.5),
    ],
)
@pytest.mark.parametrize(
    ("weighting", "reference"),
    [("A", a_weighting), ("C", c_weighting)],
)
def test_sos_response_matches_reference(
    sample_rate,
    max_frequency_hz,
    tolerance_db,
    weighting,
    reference,
):
    frequencies = STANDARD_CENTER_FREQUENCIES
    frequencies = frequencies[frequencies <= max_frequency_hz]
    sos = spl_weighting_sos(sample_rate, weighting)
    _, response = scipy_signal.sosfreqz(sos, worN=frequencies, fs=sample_rate)
    actual_db = 20.0 * np.log10(np.maximum(np.abs(response), 1e-30))
    np.testing.assert_allclose(
        actual_db,
        reference(frequencies),
        atol=tolerance_db,
        rtol=0.0,
    )
```

Also assert:

- A/C gain at 1 kHz is 0 dB within `0.01 dB`;
- repeated coefficient lookup returns the cached immutable coefficient object;
- normalized aliases such as `"A"` and `" a "` return the same cached immutable
  coefficient object;
- A and C do not share one coefficient object;
- non-positive sample rates raise `ValueError`;
- positive low sample rates return a stable coefficient set without a
  conformance claim.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_spl_weighting.py -q
```

Expected: FAIL during import because `spl_weighting.py`,
`spl_weighting_sos`, or `SplFrequencyWeightingFilter` does not exist.

- [ ] **Step 3: Implement analytic curves and cached SOS coefficient design**

Move or centralize the existing analytic A/C functions without breaking
`app.service.analysis.dsp.a_weighting` and `.c_weighting` imports.

Implement the coefficient shape:

```python
_BREAK_FREQUENCIES_HZ = (
    20.598997,
    107.65265,
    737.86223,
    12194.217,
)


def spl_weighting_sos(sample_rate: float, weighting: str) -> np.ndarray | None:
    sample_rate = float(sample_rate)
    if sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")
    normalized = str(weighting or "Z").strip().upper()
    if normalized not in ("A", "C"):
        return None
    return _cached_spl_weighting_sos(sample_rate, normalized)


@lru_cache(maxsize=None)
def _cached_spl_weighting_sos(
    sample_rate: float,
    normalized_weighting: str,
) -> np.ndarray:

    # Build A or C analog zeros/poles from the approved IEC breakpoints.
    # Prewarp each breakpoint only when all are below Nyquist; otherwise use
    # the unwarped analog prototype for callable low-rate compatibility.
    # Normalize the analog prototype, apply bilinear_zpk, then normalize the
    # digital response at 1 kHz when 1 kHz is below Nyquist.
    sos = scipy_signal.zpk2sos(digital_zeros, digital_poles, digital_gain)
    sos.setflags(write=False)
    return sos
```

Use `scipy_signal.freqs_zpk` for analog prototype normalization,
`scipy_signal.bilinear_zpk` for conversion, and
`scipy_signal.freqz_zpk` for digital 1 kHz normalization. Do not hand-expand
high-order transfer-function polynomials.

- [ ] **Step 4: Write failing state, reset, bypass, and independence tests**

Cover:

```python
def test_streaming_filter_is_chunk_invariant():
    whole = SplFrequencyWeightingFilter(48_000.0, "A").process(signal)
    chunked_filter = SplFrequencyWeightingFilter(48_000.0, "A")
    chunked = np.concatenate([
        chunked_filter.process(signal[:137]),
        chunked_filter.process(signal[137:4000]),
        chunked_filter.process(signal[4000:]),
    ])
    np.testing.assert_allclose(chunked, whole, atol=1e-12, rtol=1e-10)


def test_reset_replays_identical_output():
    weighting_filter = SplFrequencyWeightingFilter(48_000.0, "C")
    first = weighting_filter.process(signal)
    weighting_filter.reset()
    second = weighting_filter.process(signal)
    np.testing.assert_allclose(second, first)
```

Also prove:

- two filter instances maintain independent state;
- Z and invalid compatibility values bypass filtering;
- empty input returns an empty float array;
- coefficient/filter errors are not caught and relabeled as Z.

- [ ] **Step 5: Implement `SplFrequencyWeightingFilter`**

```python
class SplFrequencyWeightingFilter:
    def __init__(self, sample_rate: float, weighting: str):
        self.sample_rate = float(sample_rate)
        self.weighting = str(weighting or "Z").strip().upper()
        self._sos = spl_weighting_sos(self.sample_rate, self.weighting)
        self._zi = self._zero_state()

    def _zero_state(self) -> np.ndarray | None:
        if self._sos is None:
            return None
        return np.zeros((self._sos.shape[0], 2), dtype=np.float64)

    def process(self, samples: np.ndarray) -> np.ndarray:
        values = np.asarray(samples, dtype=np.float64).reshape(-1)
        if values.size == 0 or self._sos is None:
            return values
        filtered, self._zi = scipy_signal.sosfilt(
            self._sos,
            values,
            zi=self._zi,
        )
        return filtered

    def reset(self) -> None:
        self._zi = self._zero_state()
```

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_spl_weighting.py tests/test_dsp.py -q
```

Expected: all tests pass; existing imports of `a_weighting` and `c_weighting`
from `dsp` still work.

- [ ] **Step 7: Commit Task 1**

```powershell
git add app/service/analysis/spl_weighting.py app/service/analysis/dsp.py tests/test_spl_weighting.py
git commit -m "feat: add stateful SPL weighting filter"
```

## Task 2: Integrate Continuous Weighting Into SPL DSP

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing `SplDetector` streaming and regression tests**

Add tests that process at least two seconds of steady tones so startup state is
not mistaken for frequency response:

```python
def test_a_weighted_31_5_hz_streaming_detector_matches_reference():
    signal, sample_rate = stable_tone(31.5, 94.0, 48_000.0, duration_sec=2.0)
    detector = SplDetector(sample_rate, "A", 0.125)
    values = [
        detector.process(signal[start:start + 2400])
        for start in range(0, signal.size, 2400)
    ]
    assert np.mean(values[-5:]) == pytest.approx(54.47, abs=1.0)
```

Add comparisons for:

- A and C at 25, 31.5, 63, 100, and 1,000 Hz;
- the same samples processed using regular and irregular chunks;
- detector reset replay;
- Z output matching the existing unweighted detector contract.

- [ ] **Step 2: Run the detector tests and verify RED**

Run:

```powershell
python -m pytest tests/test_dsp.py -k "spl and (31_5 or chunk or reset)" -q
```

Expected: the 31.5 Hz or irregular-chunk test fails against the current
per-array FFT weighting.

- [ ] **Step 3: Compose the weighting filter into `SplDetector`**

Replace the per-call FFT weighting in `SplDetector.process`:

```python
self._frequency_weighting = SplFrequencyWeightingFilter(
    self.sample_rate,
    self.weighting,
)


def process(self, signal: np.ndarray) -> float:
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size == 0:
        return -np.inf
    weighted = self._frequency_weighting.process(sig)
    # Keep the existing pressure-squared exponential detector contract.


def reset(self) -> None:
    self._frequency_weighting.reset()
    self._mean_square_state = None
```

Keep `_apply_spl_frequency_weighting` as a compatibility wrapper around one
fresh `SplFrequencyWeightingFilter`, rather than retaining the FFT
implementation.

- [ ] **Step 4: Write failing bounded trace and Leq tests**

Extend the helper contracts with sample bounds:

```python
times, values = compute_spl_trace(
    full_signal,
    sample_rate,
    "A",
    0.125,
    chunk_duration=0.05,
    output_start_sample=start_sample,
    output_end_sample=end_sample,
)
```

Tests must prove:

- detector state is warmed by samples before `output_start_sample`;
- returned times are absolute from the beginning of `full_signal`;
- only endpoints strictly after the start and at or before the end are emitted;
- the final partial chunk ending at `output_end_sample` is emitted once;
- default bounds cover the full signal;
- invalid or reversed bounds normalize to a safe empty/result contract;
- `compute_spl_leq(..., start_sample=..., end_sample=...)` filters continuously
  from sample zero but averages energy only inside the selected range.

- [ ] **Step 5: Implement one-pass bounded trace and Leq helpers**

Use a single detector for the trace:

```python
for chunk_start in range(0, output_end_sample, chunk_samples):
    chunk_end = min(chunk_start + chunk_samples, output_end_sample)
    value_db = detector.process(sig[chunk_start:chunk_end])
    if chunk_end > output_start_sample:
        times_sec.append(chunk_end / sample_rate)
        values_db.append(float(value_db))
```

This loop deliberately includes the final partial chunk. It must never create
a new detector per chunk.

For Leq:

```python
weighted = SplFrequencyWeightingFilter(sample_rate, weighting).process(
    sig[:end_sample]
)
window = weighted[start_sample:end_sample]
mean_square = float(np.mean(window ** 2))
```

Retain `-np.inf` for empty energy windows and the current positive sample-rate
validation.

- [ ] **Step 6: Update pipeline integration tests**

In `tests/test_pipeline.py`, assert:

- the configured A/C weighting is used numerically, not only copied into
  `SplFrame.weighting`;
- 31.5 Hz live chunks converge to the analytic A-weighted result;
- irregular successive chunks do not reset frequency weighting;
- `AnalysisPipeline.reset()` restores both frequency and time detector state.
- two simultaneously active channel pipelines fed different prefixes do not
  share weighting delay state.

In `tests/test_test_session.py`, create two fresh sessions with A-weighted
channels and prove each `_setup_pipelines()` call creates a distinct detector
and frequency-filter state. Feed the first session a non-zero prefix and verify
the second session's first result matches a pristine detector.

- [ ] **Step 7: Run Task 2 tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_spl_weighting.py tests/test_dsp.py tests/test_pipeline.py tests/test_test_session.py -q
```

Expected: all pass, including corrected 31.5 Hz response and final-partial
trace coverage.

- [ ] **Step 8: Commit Task 2**

```powershell
git add app/service/analysis/dsp.py tests/test_dsp.py tests/test_pipeline.py tests/test_test_session.py
git commit -m "fix: stream SPL weighting across audio chunks"
```

## Task 3: Make Judgment Use Full-Capture SPL State

**Files:**
- Modify: `app/service/analysis/judgment.py`
- Modify: `tests/test_judgment.py`

- [ ] **Step 1: Write failing full-capture and shared-trace tests**

Add spy-based tests proving `JudgmentEngine` passes the complete calibrated
channel signal plus `output_start_sample` and `output_end_sample` to
`compute_spl_trace`.

```python
def fake_trace(signal, sample_rate, weighting, time_constant, **kwargs):
    captured["signal"] = np.asarray(signal).copy()
    captured["kwargs"] = kwargs
    return [1.00, 1.05], [52.0, 53.0]
```

Assert:

- the signal begins at capture sample zero, not analysis-window sample zero;
- output bounds equal the configured or rejudge sample indices;
- microphone calibration is applied before SPL processing;
- `spl_db` is the final selected trace value;
- min, max, and enabled limit decisions come from that same selected trace;
- `compute_spl` is no longer a second numeric authority for judgment;
- Leq receives the full calibrated signal and the same sample bounds.

- [ ] **Step 2: Run focused judgment tests and verify RED**

Run:

```powershell
python -m pytest tests/test_judgment.py -k "spl and (window or trace or average or limit)" -q
```

Expected: failures show the current code passes only the sliced analysis signal
and calls `compute_spl` separately.

- [ ] **Step 3: Implement one trace authority in `JudgmentEngine`**

For each valid acoustic channel:

```python
calibrated_full = dsp.apply_mic_calibration(full_sig, sensitivity)
trace_times, trace_values = dsp.compute_spl_trace(
    calibrated_full,
    sample_rate,
    spl_weighting,
    spl_time_constant,
    chunk_duration=_SPL_CHUNK_DURATION_SEC,
    output_start_sample=start_idx,
    output_end_sample=end_idx,
)
spl_db = float(trace_values[-1]) if trace_values else None
spl_min_db, spl_max_db = self._value_bounds(trace_values)
spl_average_db = dsp.compute_spl_leq(
    calibrated_full,
    sample_rate,
    spl_weighting,
    start_sample=start_idx,
    end_sample=end_idx,
)
```

If only an already-sliced fallback signal exists, use bounds `0..len(sig)`.
Preserve the existing availability/error and unconstrained-result semantics.

- [ ] **Step 4: Add numerical pre-roll and limit regression tests**

Use a full capture containing a pre-window low-frequency tone and a selected
window beginning at a non-zero offset. Compare judgment results to direct
bounded DSP helper results. Verify prefix samples warm state without becoming
trace min/max points or Leq energy.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_judgment.py tests/test_dsp.py -q
```

Expected: all pass, with final/min/max/Leq/limit values derived from continuous
weighting and the selected window.

- [ ] **Step 6: Commit Task 3**

```powershell
git add app/service/analysis/judgment.py tests/test_judgment.py
git commit -m "fix: warm SPL judgment from capture start"
```

## Task 4: Make Post-Run SPL Charts Reuse Continuous Trace Semantics

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Modify: `tests/test_review_payloads.py`

- [ ] **Step 1: Write failing full-capture chart tests**

Update/add tests proving:

- `_build_spl_chart` receives the calibrated complete channel signal;
- trace output bounds match `_AnalysisWindowSlice` sample indices;
- returned trace times are already absolute and are not offset a second time;
- samples before a non-zero window warm state but do not appear in the chart;
- the final partial chunk at the selected window end is included once.

Change the existing tests named like
`test_build_chart_payload_spl_omits_trailing_partial_tail_*` to require the
approved final-partial endpoint instead of preserving the old omission.

- [ ] **Step 2: Run focused chart tests and verify RED**

Run:

```powershell
python -m pytest tests/test_review_payloads.py -k "spl" -q
```

Expected: failures show that chart construction currently passes only the
sliced signal, offsets local times, and omits a trailing partial chunk.

- [ ] **Step 3: Carry exact sample bounds in `_AnalysisWindowSlice`**

Extend the private dataclass:

```python
@dataclass(frozen=True)
class _AnalysisWindowSlice:
    samples: np.ndarray
    start_sec: float
    end_sec: float
    start_sample: int
    end_sample: int
    has_explicit_window: bool
```

Populate these fields in every return path of
`_slice_analysis_window_with_bounds`.

- [ ] **Step 4: Build the SPL chart from the full calibrated signal**

Change `_build_spl_chart` to accept `output_start_sample` and
`output_end_sample`, call bounded `compute_spl_trace`, and serialize returned
absolute times directly.

At the caller:

```python
spl_signal = calibrated_full if calibrated_full is not None else calibrated
spl_start = window_slice.start_sample if calibrated_full is not None else 0
spl_end = window_slice.end_sample if calibrated_full is not None else len(calibrated)
```

Do not change FFT, octave, STFT, loudness, or vibration slicing behavior.

- [ ] **Step 5: Add corrected 31.5 Hz chart regression**

For a 48 kHz, 94 dB, 31.5 Hz recording configured with A weighting, assert the
stable tail of chart `values_db` is approximately `54.47 dBA` within `1.0 dB`.
Also assert the final chart time equals the selected end time when the last
chunk is partial.

- [ ] **Step 6: Run Task 4 tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_review_payloads.py tests/test_judgment.py tests/test_dsp.py -q
```

Expected: all pass; SPL chart times remain within the selected absolute
analysis range and corrected values use full-capture filter state.

- [ ] **Step 7: Commit Task 4**

```powershell
git add app/service/analysis/review_payloads.py tests/test_review_payloads.py
git commit -m "fix: preserve SPL weighting state in review charts"
```

## Task 5: Cross-Path Consistency and Full Verification

**Files:**
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_judgment.py`
- Modify: `tests/test_review_payloads.py`
- Modify: `tests/test_live_display_dispatcher.py` only if needed for reset
  coverage
- Modify: `tests/test_imported_audio_analysis.py`
- Modify implementation files from Tasks 1-4 only when a failing
  cross-path test demonstrates a spec defect

- [ ] **Step 1: Write a failing real-time/post-run consistency test**

Feed the same calibrated 48 kHz sample stream through `AnalysisPipeline` in
50 ms blocks and through bounded `compute_spl_trace`. At matching endpoints,
assert A and C values differ by no more than `0.1 dB`.

Add the concrete parameterized node:

```python
@pytest.mark.parametrize(
    ("weighting", "frequency_hz"),
    [("A", 31.5), ("A", 100.0), ("C", 31.5), ("C", 100.0), ("Z", 31.5)],
)
def test_live_spl_matches_post_run_trace_at_common_endpoints(
    weighting,
    frequency_hz,
):
    ...
```

Cover:

- 31.5 Hz and 100 Hz steady tones;
- a deterministic multi-tone signal;
- a final partial block;
- Z weighting as the unchanged control.

- [ ] **Step 2: Run the cross-path test and verify RED or justified GREEN**

Run the exact new node:

```powershell
python -m pytest tests/test_pipeline.py::test_live_spl_matches_post_run_trace_at_common_endpoints -v
```

Expected: FAIL if any integration mismatch remains. If it is already GREEN
because Tasks 1-4 fully satisfy the contract, record that result in the task
commit message/body and do not introduce production changes merely to force a
failure.

- [ ] **Step 3: Add or confirm sample-gap reset integration coverage**

Use the existing `LiveDisplayDispatcher` gap behavior to prove a pipeline reset
restarts the complete SPL detector. Prefer extending an existing gap/reset
test. Do not add dispatcher production logic unless the current reset callback
is not invoked.

- [ ] **Step 4: Add fresh imported-analysis lifecycle coverage**

In `tests/test_imported_audio_analysis.py`, run the production
`analyze_imported_wav` path twice for the same A-weighted WAV/config. Ensure the
first run cannot leave weighting state that changes the second run's SPL chart
or judgment values. The two result payloads must contain equal SPL series and
summary values.

- [ ] **Step 5: Make the minimal integration correction**

If the cross-path or gap test fails, correct only the demonstrated boundary:
sample indexing, final partial handling, reset propagation, or duplicated time
offset. Do not alter unrelated chart or dispatcher behavior.

- [ ] **Step 6: Run all focused analysis tests**

Run:

```powershell
python -m pytest tests/test_spl_weighting.py tests/test_dsp.py tests/test_pipeline.py tests/test_test_session.py tests/test_live_display_dispatcher.py tests/test_judgment.py tests/test_review_payloads.py tests/test_imported_audio_analysis.py -q
```

Expected: all pass.

- [ ] **Step 7: Run the complete test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass with no regressions.

- [ ] **Step 8: Verify repository scope**

Run:

```powershell
git status --short
git diff --check
$splPlanBase = git log -1 --format=%H -- docs/double-sdd/plans/2026-07-24-streaming-spl-frequency-weighting.md
git diff --stat "$splPlanBase..HEAD"
```

Expected: the range begins at the committed implementation plan and includes
all implementation task commits. Only SPL weighting implementation/tests and
the approved plan/spec history are present; no unrelated user changes are
staged or committed.

- [ ] **Step 9: Commit Task 5**

```powershell
git add tests/test_pipeline.py tests/test_test_session.py tests/test_judgment.py tests/test_review_payloads.py tests/test_live_display_dispatcher.py tests/test_imported_audio_analysis.py app/service/analysis/spl_weighting.py app/service/analysis/dsp.py app/service/analysis/judgment.py app/service/analysis/review_payloads.py
git commit -m "test: verify continuous SPL weighting end to end"
```

If no files changed in Task 5 because all new tests were committed with the
preceding integration task, do not create an empty commit.
