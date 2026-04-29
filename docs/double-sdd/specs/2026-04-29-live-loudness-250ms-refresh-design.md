# Live Loudness 250ms Refresh Design

## Purpose

The homepage result-summary live loudness badge currently refreshes at most once every `0.5s`. User feedback says this still feels too slow. Change live loudness refresh to match the SPL metric badge display cadence: at most once every `0.25s`.

## Approved Decisions

1. Live loudness throttle changes from `0.5s` to `0.25s`.
2. The rolling loudness analysis window remains `0.5s`; this preserves the existing smoothing/input duration while allowing more frequent updates.
3. Live loudness is still computed only for the currently displayed channel, as implemented by the homepage metrics performance follow-up.
4. SPL behavior is unchanged: the result-summary SPL badge UI remains throttled at `0.25s`, while the underlying SPL signal cadence remains unchanged.
5. Post-analysis average SPL/loudness behavior is unchanged and continues to use serialized judgment results for the analysis window.

## Scope

In scope:

- Update the live loudness throttle constant.
- Update tests that document the throttle/window values and live loudness emission cadence.
- Update the existing homepage metrics performance spec/plan references so docs match the implementation.

Out of scope:

- Changing the live loudness rolling window.
- Changing the loudness algorithm.
- Changing channel-selection memory behavior.
- Changing SPL cadence or SPL UI throttling.
- Changing final average SPL/loudness computation.

## Runtime Contract

`AnalysisPipeline` must use:

- `_LIVE_LOUDNESS_THROTTLE_SEC = 0.25`
- `_LIVE_LOUDNESS_WINDOW_SEC = 0.5`

Given valid non-empty chunks and `compute_loudness=True`, `_maybe_compute_live_loudness()` may emit a `LoudnessFrame` when:

```text
elapsed_time - last_live_loudness_emit_time >= 0.25
```

Calls before the `0.25s` threshold must return `None` and avoid calling `dsp.compute_loudness_trace()`.

The first live loudness frame is still controlled by the same throttle comparison from the initial `last_live_loudness_emit_time = 0.0`. Therefore a chunk at `elapsed_time=0.25` may emit; a chunk before `0.25` may not.

## Compatibility / Migration

- Backward compatibility: required.
- Protected surfaces: existing public `AnalysisPipeline.process_chunk()` call shape, live channel gating behavior, existing judgment payload format, existing result-summary average behavior, existing SPL chart/signal behavior.
- Allowed breakage: none intended.
- Migration strategy: none.

## Verification

Run focused verification:

```powershell
pytest tests/test_pipeline.py -q
```

Run result-summary confidence verification:

```powershell
pytest tests/test_result_module.py -q
```
