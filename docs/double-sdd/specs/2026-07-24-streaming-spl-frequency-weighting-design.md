# Streaming SPL Frequency Weighting Design

## Context

The SPL configuration already propagates `spl_weighting` from the channel
configuration into the real-time `AnalysisPipeline` and its `SplDetector`.
The defect is therefore not missing configuration propagation.

The current detector applies A/C frequency weighting by taking an FFT of every
input array, multiplying the bins by the requested weighting curve, and taking
an inverse FFT. Real-time input arrays are recorder blocks of approximately
50 ms. At 48 kHz, each block therefore has only 20 Hz FFT-bin spacing.
Low-frequency tones that do not land on those bins leak into neighboring bins,
where substantially different A/C corrections are applied.

The observed regression is material:

- A 94 dB, 100 Hz tone produces the expected A-minus-Z correction of about
  -19.15 dB.
- A 94 dB, 31.5 Hz tone should produce about 54.47 dBA, but the current
  50 ms real-time path produces approximately 74-75 dBA.
- Increasing the FFT block to the loudness window duration improves some
  inputs but remains dependent on bin alignment and does not reliably correct
  the defect.

The approved direction is to replace per-block FFT weighting for SPL with a
continuous, stateful A/C weighting filter shared by real-time and post-run SPL
analysis.

## Goals

1. Apply A/C SPL frequency weighting continuously across recorder block
   boundaries.
2. Make steady-state SPL results independent of input chunk size within
   0.1 dB.
3. Use one weighting implementation for:
   - the real-time SPL metric;
   - the post-run SPL chart;
   - SPL minimum, maximum, and limit judgment;
   - final and equivalent-continuous SPL calculations.
4. Preserve the configured Fast/Slow exponential time weighting behavior.
5. Keep Z weighting behavior unchanged.
6. Preserve the existing 50 ms processing cadence and 250 ms display emission
   cadence without adding a loudness-sized SPL display delay.
7. Meet the approved frequency-response acceptance limits:
   - 44.1 and 48 kHz: 20 Hz through 12.5 kHz within +/-1.0 dB;
   - 96 kHz: 20 Hz through 20 kHz within +/-1.5 dB;
   - 1 kHz normalized to 0 dB.

## Non-Goals

- Do not change SPL configuration fields, allowed weighting values, UI controls,
  database schema, config packages, or public payload field names.
- Do not recalculate or migrate historical test results.
- Do not change FFT-spectrum weighting, octave weighting, loudness analysis,
  microphone calibration, recorder cadence, or UI refresh cadence.
- Do not claim that the complete measurement system is IEC 61672 class
  certified. Microphone, calibration, acquisition hardware, and end-to-end
  tolerance remain outside this change.
- Do not introduce unrelated DSP or analysis refactors.

## User-Visible Behavior

- New A- and C-weighted tests report corrected real-time and post-run SPL
  values, especially for low-frequency content.
- Real-time and post-run values derived from the same samples use the same
  continuous weighting response.
- The first real recorder samples are processed with a causal filter starting
  from reset state. No synthetic pre-roll, hidden sample removal, or additional
  display wait is introduced.
- Samples that occur before the configured analysis window act as real filter
  pre-roll for post-run analysis. If the analysis window begins at sample zero,
  its initial causal filter transient remains part of the measurement.
- The final short recorder block is processed with the existing filter state.
  The implementation must not reset the filter, pad synthetic samples, or
  weight the final block independently.
- Z-weighted results retain the existing flat response and timing behavior.

## Design

### 1. Stateful frequency-weighting component

Add an internal SPL frequency-weighting component with these responsibilities:

- accept `sample_rate` and `weighting` (`A`, `C`, or `Z`);
- design or retrieve immutable second-order-section (SOS) coefficients;
- hold `scipy.signal.sosfilt` delay state for one signal stream;
- process arbitrary one-dimensional chunks while preserving state;
- reset its state deterministically;
- bypass filtering for Z weighting.

Filter state is owned by one channel in one test run. State must never be
shared across channels, sessions, imported files, or concurrent analyses.
Only immutable coefficient sets may be cached.

### 2. Coefficient design

Use the same IEC A/C analog pole and zero definitions represented by the
existing `a_weighting` and `c_weighting` reference functions.

For supported production sample rates where the highest A/C breakpoint is
below Nyquist:

1. Prewarp each analog breakpoint for the active sample rate.
2. Convert the analog zero/pole/gain representation with the bilinear
   transform.
3. Convert the digital result to SOS form.
4. Normalize digital gain at 1 kHz.
5. Cache the immutable SOS coefficients by normalized
   `(sample_rate, weighting)`.

For positive sample rates below the declared 44.1/48/96 kHz acceptance set,
retain callable behavior without claiming the production tolerance:

- when individual breakpoint prewarping is not defined because a breakpoint is
  at or above Nyquist, use the unwarped analog prototype with the bilinear
  transform;
- normalize digitally at 1 kHz when 1 kHz is below Nyquist;
- otherwise retain the analog prototype normalization.

Non-positive sample rates remain errors. Coefficient generation or filtering
failure must surface through the existing analysis error paths; it must never
silently fall back from A/C to Z.

### 3. SPL detector composition

Separate the two stateful DSP responsibilities conceptually:

1. frequency weighting converts microphone-calibrated pressure samples into
   A-, C-, or Z-weighted pressure samples;
2. exponential pressure-squared detection applies the configured Fast/Slow time
   constant and converts the result to dB re 20 uPa.

`SplDetector` remains the streaming facade used by `AnalysisPipeline`. It owns
both states and preserves its current construction and `process`/`reset`
contract where practical.

`SplDetector.reset()` must reset both frequency-filter state and
pressure-squared state. The existing live-display sample-gap reset therefore
resets the complete detector without new dispatcher policy.

### 4. Real-time data flow

The real-time path remains:

1. receive the recorder block;
2. select the logical channel;
3. apply microphone calibration;
4. pass the calibrated samples through that channel's stateful SPL detector;
5. create `SplFrame` with the configured weighting metadata;
6. emit the latest frame subject to the existing 250 ms throttle.

Every input sample must be processed exactly once and in order. Display
throttling may discard intermediate frames for presentation, but it must not
discard samples from detector state.

### 5. Post-run and judgment data flow

Post-run SPL consumers must not restart frequency weighting at each 50 ms chart
chunk or at the configured analysis-window boundary.

For each calibrated channel:

1. start the weighting filter at the beginning of the complete captured
   recording;
2. process samples continuously in their original order;
3. use samples before the analysis window as filter pre-roll;
4. select the configured analysis-window samples or trace points only after
   continuous weighting/detection;
5. derive chart values, minimum, maximum, final level, Leq, and limit decisions
   from the selected weighted data.

The implementation may expose a reusable already-weighted-signal helper or a
continuous detector trace helper, but it must not maintain separate A/C
algorithms for real-time and post-run paths.

Post-run trace chunking controls output sampling only. It must not restart the
frequency filter or make frequency response depend on trace chunk duration.

### 6. State boundaries and failures

- New session or imported-file analysis: create fresh state.
- Channel change: use the independent state already owned by that channel.
- Explicit pipeline reset or detected live sample gap: reset both SPL states.
- Final partial block: process normally with existing state.
- Empty input: preserve current public empty-input behavior.
- Invalid weighting from an internal caller: preserve current flat/bypass
  compatibility unless configuration validation rejects it earlier.
- Numerical or coefficient errors: report the error; do not emit a plausible
  Z-weighted value labeled as A or C.

## Acceptance Criteria

### Frequency response

Measure steady-state sinusoidal response against the existing analytic
`a_weighting` and `c_weighting` reference functions at standard acoustic center
frequencies.

- At 44.1 and 48 kHz, every tested frequency from 20 Hz through 12.5 kHz is
  within +/-1.0 dB.
- At 96 kHz, every tested frequency from 20 Hz through 20 kHz is within
  +/-1.5 dB.
- A and C response at 1 kHz is 0 dB within a tight numerical tolerance.
- A 94 dB, 31.5 Hz steady tone at 48 kHz is approximately 54.47 dBA within the
  approved +/-1.0 dB response tolerance.

### Streaming behavior

- Processing identical samples as one array or as different regular and
  irregular chunk sequences changes stable SPL results by no more than 0.1 dB.
- A chunk boundary through a sinusoid does not introduce a periodic level
  spike.
- Resetting and replaying the same samples reproduces the same result.
- A detected sample gap resets frequency and time states.
- The final partial chunk is included once and does not cause a state reset.
- Z-weighted sample and SPL behavior remains equivalent to the pre-change flat
  path within normal floating-point tolerance.

### Real-time/post-run consistency

- For the same calibrated captured samples and analysis interval, real-time
  frames and post-run trace points at matching sample endpoints agree within
  0.1 dB, except that the UI may emit only the throttled subset.
- Samples before a non-zero analysis-window offset warm the post-run filter but
  do not appear as chart points or enter window-only summary statistics.
- Chart minimum/maximum and limit judgment use the same trace values.
- Leq uses the continuously frequency-weighted samples selected for the
  analysis interval.

## Testing Strategy

Add focused tests for:

- A/C SOS frequency response at 44.1, 48, and 96 kHz;
- 25, 31.5, 63, 100, and 1,000 Hz regression tones;
- regular, irregular, single-array, and final-partial-chunk processing;
- state reset and live-display gap handling;
- independent state for multiple channels and sessions;
- Z bypass behavior;
- real-time pipeline frames versus post-run trace values;
- analysis-window pre-roll and exclusion from displayed/statistical bounds;
- judgment minimum, maximum, Leq, and enabled limit decisions;
- current error behavior for invalid sample rate, time constant, and empty
  input.

Run the focused DSP, pipeline, judgment, review-payload, session, and dispatcher
tests, followed by the complete test suite.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces:
  - SPL configuration keys and allowed `A`, `C`, and `Z` values.
  - UI controls and visible refresh cadence.
  - Database schema and config-package formats.
  - Public frame, review payload, and judgment payload field names.
  - Z-weighted behavior.
  - Existing historical test records and reports.
- Allowed breakage:
  - Newly calculated A/C SPL numbers may change where the old per-block FFT
    implementation was inaccurate.
  - Tests or fixtures that encoded inaccurate A/C numeric baselines must be
    updated to the approved analytic response.
- Migration strategy: none. Existing stored results are not recalculated;
  corrected weighting applies only to newly executed or explicitly reanalyzed
  measurements.
