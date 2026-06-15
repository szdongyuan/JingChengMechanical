# FFT Weighting Design

## Context

The application already supports IEC-style `A`, `C`, and `Z` weighting for SPL
and 1/3-octave analysis. FFT spectrum analysis currently has no weighting
choice: the configured FFT resolution, x-axis mode, limit envelope, and golden
sample behavior all operate on the unweighted FFT spectrum.

The requested behavior is to add a selectable weighting mode for FFT spectrum
analysis. The weighting must be unified: the FFT spectrum graph, FFT limit
judgment, and FFT golden sample comparison must all use the same weighted FFT
levels for a channel.

## Goals

1. Add a per-channel FFT weighting configuration with `A`, `C`, and `Z` choices.
2. Use the same FFT weighting for:
   - live FFT frames
   - review/homepage FFT spectrum payloads
   - FFT band-limit judgment
   - FFT golden sample absolute comparison
   - FFT golden sample delta comparison
   - optional FFT golden overlay curves
3. Default new channel configurations to `Z` weighting.
4. Reuse the existing IEC 61672 weighting functions already used by DSP code.
5. Update the built-in prepared SQLite database schema so the shipped database
   includes the new field.
6. Keep the feature per channel, matching the existing `fft_nfft`,
   `fft_x_axis_scale`, and `oct_weighting` patterns.

## Non-Goals

- Do not add a global FFT weighting setting.
- Do not add weighting choices beyond `A`, `C`, and `Z`.
- Do not change STFT, SPL, loudness, or octave behavior.
- Do not add runtime database migration code.
- Do not preserve old FFT numeric results for existing local development
  databases that lack the new schema.
- Do not change FFT limit table semantics, interpolation, x-axis behavior, or
  golden sample modes except for applying the selected weighting to the
  measured and golden FFT levels used by those features.

## User-Visible Behavior

Each channel's FFT parameter card exposes a `计权模式` control near the existing
FFT controls.

Allowed values:

- `A`
- `C`
- `Z`

The default value is `Z`.

Changing the FFT weighting changes the FFT curve and the FFT values used for
limit checks. If a golden sample mode is enabled, the golden sample FFT is
computed using the same weighting before absolute or delta comparison.

For delta mode:

- measured FFT levels are weighted
- golden FFT levels are weighted
- the plotted and judged value is `weighted measured - weighted golden`
- configured FFT limits remain delta-space lower/upper bounds

For absolute mode:

- measured FFT levels are weighted
- golden overlay levels are weighted
- configured FFT limit values remain offsets relative to the weighted golden
  curve
- evaluated lower/upper limits are `weighted golden + configured offset`
- limit checks judge weighted measured absolute levels against those evaluated
  absolute limits

For golden overlay with mode off:

- the measured FFT curve is weighted
- the optional golden overlay curve is weighted

## Technical Design

### Configuration Model

Add a new per-channel field:

- storage key: `fft_weighting`
- type: text
- allowed values: `A`, `C`, `Z`
- default: `Z`

The field is added to:

- `model/channel_config_table.py`
- `app/service/config_service.py` channel defaults
- config load and save paths
- import normalization
- validation
- package/export payloads where channel dicts naturally include all channel
  fields
- UI collect/populate logic in `ui/modules/config.py`

Validation rejects values outside `A`, `C`, and `Z` with a channel-scoped error
message.

### Database Strategy

The project ships a prepared SQLite database. Because the program has not
launched and backward compatibility is not required, no runtime migration code
will be added.

The implementation must instead:

- update the ORM model
- update startup schema checks so `fft_weighting` is a required prepared
  database column
- update tests that synthesize prepared runtime schemas
- update the built-in `data/jingchen.db` database so `channel_config_table`
  contains `fft_weighting TEXT NOT NULL DEFAULT 'Z'`

This feature scope updates the canonical prepared database at `data/jingchen.db`.
Existing database copies under generated release-package directories are not
manually edited as part of this feature. A release packaging task must regenerate
or verify any packaged database copies from the canonical prepared database
before shipping an installer or portable bundle.

### DSP

Extend `dsp.compute_fft_spectrum` to accept an optional `weighting` argument,
defaulting to `Z`.

The existing FFT magnitude computation remains unchanged through the linear
magnitude and dB conversion steps. After `freqs` and `mag_db` are computed:

- `Z`: add `0 dB` at every frequency
- `A`: add `a_weighting(freqs)` to `mag_db`
- `C`: add `c_weighting(freqs)` to `mag_db`

The existing weighting functions clamp the raw weighting ratio before converting
to dB, so DC becomes a very large finite attenuation instead of `-inf`.
Downstream FFT chart payload construction must still filter frequency/level
pairs together wherever finite filtering is needed so future non-finite values
cannot create mismatched `frequencies_hz` and `levels_db` arrays.

### Judgment

`JudgmentEngine._check_fft_bands` reads `fft_weighting` from the channel config
and passes it to measured FFT calculation and golden FFT calculation.

The same weighting is used before:

- finite input filtering
- limit envelope evaluation
- golden axis matching
- delta calculations
- serialized `BandResult` values

### Review Chart Payload

`review_payloads._build_fft_chart` reads `fft_weighting` from the channel config
and passes it into measured FFT and golden FFT derivation.

The chart payload may include a `weighting` field for clarity. Existing chart
widgets do not need special weighting logic; they render the already-weighted
series.

When filtering non-finite weighted values, the payload builder must filter:

- frequency and measured level pairs together
- frequency, measured level, and golden level triples together whenever golden
  levels participate in chart computation, including delta mode, absolute mode,
  and optional overlay rendering

### Golden Reference Cache

Golden FFT references must be cached by both FFT resolution and weighting.

Required cache key:

- `(nfft, weighting)`

This prevents a golden FFT computed with `Z` weighting from being reused for `A`
or `C` weighting in the same run.

Octave golden caching is unchanged.

### Live FFT Pipeline

`AnalysisPipeline` reads `fft_weighting` from the channel config and passes it to
`dsp.compute_fft_spectrum` for live `FftFrame` output.

The `FftFrame` dataclass may include a `weighting` field defaulting to `Z` so
downstream consumers can inspect the frame's weighting without breaking simple
construction sites.

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces: current user-visible FFT behavior after this feature,
  prepared database startup schema, config editor save/load behavior, result
  judgment consistency
- Allowed breakage: local or packaged databases that do not contain the new
  `fft_weighting` column may fail startup schema checks
- Migration strategy: none; update and ship a prepared database containing the
  new column

## Test Strategy

Add or update focused tests for:

1. `compute_fft_spectrum(..., weighting="A")` attenuates low-frequency FFT bins
   relative to `Z`.
2. `compute_fft_spectrum(..., weighting="C")` applies C-weighting and remains
   flatter than A-weighting at low frequency.
3. Config UI restores and collects `fft_weighting`.
4. Config service validates, stores, and loads `fft_weighting`.
5. Prepared database startup checks require the `fft_weighting` column.
6. FFT judgment passes `fft_weighting` into measured FFT calculation.
7. FFT golden comparison passes the same `fft_weighting` into golden FFT
   calculation.
8. Golden reference cache separates cached FFTs by `(nfft, weighting)`.
9. Review chart payload uses weighted FFT values and filters any non-finite
   frequency/level pairs without array length mismatch.
10. Live `AnalysisPipeline` passes `fft_weighting` to live FFT calculation.

## Risks

- Existing configured FFT limit values may need recalibration after switching
  from `Z` to `A` or `C`, because limits are judged against weighted dB values.
- DC is currently represented as a very large finite attenuation for A/C
  weighting; chart payload and widget boundaries must still handle any future
  non-finite FFT values safely.
- Prepared database copies outside `data/jingchen.db` can drift if a later
  packaging task does not regenerate or verify them from the canonical database.
