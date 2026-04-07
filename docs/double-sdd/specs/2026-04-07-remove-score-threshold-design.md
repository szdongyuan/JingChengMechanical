# Remove `score_threshold` And Unify AI Threshold Semantics

## Summary

The current AI result path uses two different threshold concepts:

- `decision_threshold` inside the external toolchain
- `score_threshold` inside the main app binding

This split causes confusing behavior. The toolchain can return a real numeric `score` and `FAIL`, while the main app summary still shows no AI deviation or treats the AI row as unconstrained because `score_threshold` is unset.

This pass removes `score_threshold` entirely and defines one threshold interpretation for AI results in the main app:

- the effective threshold is the toolchain's actual `decision_threshold`
- when the toolchain returns that effective threshold in `ai_result.extra.decision_threshold`, the app uses it directly
- if that value is unavailable, the app falls back to request `params_json.decision_threshold`
- if neither exists, the app falls back to `0.5`

The same effective threshold must drive:

- AI pass/fail consumption in the main app
- AI deviation display in the result summary
- AI row `OK` / `NG` status in the result summary

`ai_label` remains a fallback only for cases where a usable numeric `score` is unavailable.

## Problem

### Split Threshold Semantics

The current implementation uses two threshold fields with overlapping meaning:

- the toolchain interprets `score` with `decision_threshold`
- the main app interprets `score` with `score_threshold`

This is redundant for the current product and violates YAGNI.

### User-Facing Inconsistency

The result summary computes AI deviation only when `score_threshold` is present. If it is absent, the AI row becomes unconstrained even when:

- the AI toolchain executed successfully
- the toolchain returned a numeric `score`
- the toolchain returned `FAIL`

This makes the summary misleading and makes users think AI did not run or was not consumed.

### Dead Product Surface

`score_threshold` exists in the database and service layer but is not exposed in the current config UI. That makes it both real enough to affect behavior and hidden enough to confuse operators and developers.

## Scope

In scope:

- remove `score_threshold` from the app's product semantics
- remove `score_threshold` from the binding model and service contract
- remove `score_threshold` from schema bootstrap / migration logic
- destructively remove the `score_threshold` column from existing runtime databases on startup
- unify AI pass/fail and deviation calculation around one effective threshold
- define effective threshold fallback rules with default `0.5`
- update tests to reflect the single-threshold model

Out of scope:

- changing the external AI request/response protocol version
- changing toolchain packaging or process model
- adding a dedicated threshold editor UI
- adding new AI result views beyond the current summary semantics
- supporting multi-threshold or non-binary-classification interpretation in this pass

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - numeric `score` returned by AI toolchains
  - optional `ai_label` fallback behavior for score-less outputs
  - existing `params_json.decision_threshold` request contract
  - existing toolchain response `ai_result.extra.decision_threshold` field when present
- Allowed breakage:
  - removal of `score_threshold` from database schema
  - removal of `score_threshold` from service-layer binding payloads
  - removal of tests and code paths that depend on app-side threshold overrides
- Migration strategy:
  - destructive startup schema rewrite of `test_config_ai_binding_table` to remove `score_threshold`
  - existing stored `score_threshold` values are discarded and not migrated

## Approved Decisions

1. Remove `score_threshold` completely from the product model.
2. Keep only one AI score interpretation in the main app.
3. The effective threshold is resolved in this priority order:
   - valid `ai_result.extra.decision_threshold`
   - valid request `params_json.decision_threshold`
   - default `0.5`
4. The same effective threshold is used for:
   - computing AI pass/fail from numeric `score`
   - computing AI deviation in the summary
   - assigning the AI row `OK` / `NG` status
5. If numeric `score` is unavailable, the app falls back to `ai_label`.
6. No compatibility layer is required for old `score_threshold` data.
7. Existing runtime databases should be cleaned up automatically on startup instead of requiring manual reset.

## Design Summary

The main app should stop treating AI score interpretation as a test-config binding concern. Instead, AI score interpretation becomes part of AI execution consumption:

- the toolchain produces a numeric `score`
- the toolchain may also report the threshold it actually used
- the main app resolves one effective threshold for the current run
- the main app uses that one threshold everywhere it needs to interpret `score`

This removes the possibility that one layer says `FAIL` while another layer treats the same AI result as unconstrained or informational.

## Threshold Resolution

### Effective Threshold Definition

For a successful AI run, the main app resolves an effective threshold as follows:

1. If `ai_result.extra.decision_threshold` exists and is a finite numeric probability in `[0.0, 1.0]`, use it.
2. Else, if request `params_json.decision_threshold` exists and is a finite numeric probability in `[0.0, 1.0]`, use it.
3. Else, use `0.5`.

The resolved value is the effective threshold for that run.

If a candidate threshold is missing, non-numeric, `NaN`, `inf`, or outside `[0.0, 1.0]`, it is ignored and the resolver proceeds to the next fallback source.

### Why Response First

The response field is the most authoritative value because it reflects the threshold the toolchain actually used after its own internal fallback logic. For example, a toolchain may ignore missing request params and fall back to model metadata. The app should consume the actual runtime threshold, not guess it.

## Runtime Consumption Changes

### `AiService.run_ai_analysis`

On successful validation, `AiService.run_ai_analysis` already extracts:

- `ai_label`
- `score`
- `summary`
- `confidence`
- `extra`

This pass makes `AiService.run_ai_analysis` the single owner of threshold resolution for successful AI runs. Downstream consumers must not reimplement the fallback chain.

The execution envelope should carry:

- `score`
- `ai_label`
- fully resolved `decision_threshold` for the current run
- threshold provenance when cheap to retain, if useful for debugging

For successful validated runs:

- `envelope["decision_threshold"]` must always be present
- its value must already reflect the full fallback chain:
  - response extra
  - request params
  - default `0.5`

For non-success runs:

- `decision_threshold` may be absent
- consumers must not invent a threshold for failed executions

### `TestSession._run_ai`

`TestSession._run_ai` must stop reading binding `score_threshold`.

New behavior:

1. Read numeric `score` from the AI envelope.
2. Read the already resolved effective threshold from `envelope["decision_threshold"]` when present.
3. If numeric `score` exists, determine `ai_pass` from `score >= effective_threshold`.
4. If numeric `score` does not exist, fall back to `ai_label`.
5. For successful validated runs, store the effective threshold in `last_ai_review["threshold"]` even if numeric `score` is absent.
6. Use this `ai_pass` to patch the final judgment result.

This ensures the result payload and the summary consume the same threshold interpretation.

### Failure Cases

If the AI run does not succeed:

- preserve current failure handling and error messaging
- do not invent a threshold for failed runs that do not provide a usable score
- continue to show AI `无结果` when execution/validation fails

If the AI run succeeds but returns no usable numeric `score`:

- keep current fallback to `ai_label`
- keep `last_ai_review["score"] = None`
- keep `last_ai_review["threshold"]` set to the resolved threshold for observability
- do not compute deviation from a missing score

If a successful validated run somehow has neither usable numeric `score` nor usable `ai_label`:

- treat it as an impossible defensive path under the current validated protocol
- degrade to AI `无结果` behavior with an explicit error message instead of silently passing

Under the current protocol validator, this state should not normally occur because successful validated responses already require `ai_label` to be present and string-typed.

## Result Summary Behavior

### AI Deviation

The AI summary row should compute deviation from:

- `actual = score`
- `lower = effective_threshold`
- `upper = None`

This yields:

- positive deviation when `score < threshold` => `NG`
- zero or negative deviation when `score >= threshold` => `OK`

### No More Unconstrained AI Row For Successful Numeric AI

A successful AI run with a usable numeric `score` must no longer render as an unconstrained AI row just because `score_threshold` is absent.

Instead:

- the app resolves the effective threshold
- the app computes deviation
- the AI row participates in overall summary status normally

### `ai_label` Fallback

If the run lacks usable numeric `score` but includes `ai_label`:

- `PASS` => `ai_pass = True`
- `FAIL` => `ai_pass = False`

If `ai_label` is present but is not a recognized binary label:

- any value other than case-insensitive `PASS` / `FAIL` is treated as unusable for fallback
- the app must not silently treat unknown labels as pass
- the run should degrade to AI `无结果` behavior with an explicit error message when score fallback is needed but `ai_label` is unusable

This fallback affects final pass/fail judgment, but does not create a synthetic deviation number when no score exists.

## Data Model And Schema Changes

### Binding Model

Remove `score_threshold` from `TestConfigAiBindingTable`.

### Binding Service Contract

Remove `score_threshold` from:

- `resolve_binding()` output
- `save_binding()` validation and persistence
- create/update SQL paths

Legacy callers that still send `score_threshold` to binding save APIs must receive a validation error. The field is removed, not silently ignored.

### Runtime Database Cleanup

Startup DB bootstrap must remove `score_threshold` from `test_config_ai_binding_table` if present.

Because backward compatibility is not required, destructive cleanup is allowed. The cleanup should preserve all remaining binding data while removing the obsolete column.

The design requirement is:

- the final schema no longer contains `score_threshold`
- startup reaches that schema automatically on existing local databases
- no old value preservation is required

Implementation may use table rebuild or direct column drop, but the behavior above is required.

## Testing

Required regression coverage:

- AI final judgment uses response `decision_threshold` when present
- AI final judgment falls back to request `params_json.decision_threshold`
- AI final judgment falls back to default `0.5` when no threshold is provided elsewhere
- response `extra.decision_threshold` values that are non-numeric, non-finite, or outside `[0.0, 1.0]` are ignored
- invalid response threshold falls back to valid request threshold
- missing response threshold plus invalid request threshold falls back to default `0.5`
- invalid response threshold plus invalid/missing request threshold falls back to default `0.5`
- successful numeric AI results produce summary deviation without any binding `score_threshold`
- successful numeric AI results no longer render an unconstrained `OK` AI row solely because binding threshold is absent
- score-less successful AI results still fall back to `ai_label`
- score-less successful AI results with unknown non-binary `ai_label` degrade to AI `无结果`, not silent pass
- binding persistence rejects incoming `score_threshold`
- binding resolution no longer returns `score_threshold`
- startup DB bootstrap removes `score_threshold` from legacy binding tables

## Non-Goals

- adding support for arbitrary score transforms
- supporting multi-class threshold matrices
- preserving per-config threshold override semantics
- retaining dead schema only for historical comfort
