# Homepage Left Axis Ticks And dB Title Correction Design

## Context

The current homepage UI branch hides the whole left y-axis for analysis charts.
That removed the vertical-axis title, but it also removed y-axis tick marks and
tick values. The desired correction is narrower: keep the y-axis scale visible
for reading chart values, but do not show the vertical-axis title text.

The same branch also titled FFT and octave cards as `声压级dB`. The requested
correction is that both vertical units should be shown simply as `dB`.

## Goals

1. Restore the left y-axis tick marks and tick values on homepage analysis
   charts.
2. Keep the left y-axis title/label text hidden on homepage analysis charts.
3. Keep bottom x-axes visible and unchanged.
4. Change the homepage FFT card title to `FFT频谱（dB）`.
5. Change the homepage octave card title to `倍频程（dB）`.
6. Preserve `PASS`, `FAIL`, `OK`, and `NG` display exactly as-is.

## Non-Goals

- Do not change chart calculations, ranges, overlays, STFT geometry, judgment
  logic, payloads, database schema, or configuration formats.
- Do not remove y-axis ticks or tick values.
- Do not translate approved technical abbreviations or units.
- Do not change the SPL card title; it remains `声压级（dB）`.
- Do not change the STFT or loudness title behavior except as needed to keep
  their left-axis titles hidden while ticks remain visible.

## User-Visible Scope

Affected homepage chart modules:

- `StftModule`
- `SpectrumModule`
- `SplModule`
- `LoudnessModule`
- `OctaveModule`

Acceptance criteria:

- Each homepage chart's left axis is visible.
- Left-axis tick marks and tick numbers are visible.
- The left-axis title text is blank or otherwise not displayed.
- Bottom axes remain visible.
- FFT homepage title is `FFT频谱（dB）`.
- Octave homepage title is `倍频程（dB）`.
- SPL homepage title remains `声压级（dB）`.
- STFT homepage title remains `STFT时频图（频率Hz，幅值dB）`.
- Loudness title remains dynamic: `响度`, `响度（sone）`, or `响度（phon）`.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces:
  - Analysis and judgment behavior.
  - AI request/response protocol fields.
  - Summary payload fields and stored data.
  - Database schema and config/package formats.
  - `PASS`, `FAIL`, `OK`, and `NG` result/status labels.
- Allowed breakage:
  - UI tests that asserted the whole left axis was hidden must change to assert
    only the left-axis title is hidden while ticks remain visible.
  - Homepage FFT and octave title expectations change from `声压级dB` to `dB`.
- Migration strategy: none. This is a UI-only presentation correction.

## Design

Replace the previous homepage opt-in behavior from "hide the entire left axis"
to "hide only the left-axis title text." The generic chart widgets should keep
their default behavior unchanged unless the homepage modules opt in.

Implementation options:

1. Rename the constructor option to an axis-title-specific name such as
   `hide_left_axis_label`, then update homepage modules and tests.
2. Keep the existing constructor option but change its behavior.

Recommendation: use option 1 if practical. The new name better matches the
product behavior and avoids future confusion. A compatibility alias is optional
because this option was introduced only for this homepage UI branch and current
repo usage is limited to homepage modules and tests.

The helper should:

- leave `plot_item.getAxis("left").isVisible()` true;
- leave the bottom axis visible;
- clear or suppress the left-axis title text after any internal `setLabel("left",
  ...)` call;
- avoid changing viewbox range policies or payload APIs.

Update card title construction in `app/main_window.py`:

- `FFT频谱（声压级dB）` -> `FFT频谱（dB）`
- `倍频程（声压级dB）` -> `倍频程（dB）`

Update tests to cover the corrected semantics instead of the old full-axis-hide
behavior.

## Testing Strategy

Run focused tests for:

- default chart widgets still show normal left-axis labels by default;
- opted-in homepage chart widgets keep the left axis visible while hiding only
  the left-axis title;
- homepage chart modules use the corrected opt-in behavior;
- homepage card titles use `FFT频谱（dB）` and `倍频程（dB）`;
- existing range and STFT geometry tests continue passing.

Required verification:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_committed_chart_modules.py tests/test_main_window.py tests/test_result_module.py -q
```

