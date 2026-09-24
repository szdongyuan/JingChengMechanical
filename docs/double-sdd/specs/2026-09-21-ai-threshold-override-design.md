# Optional AI threshold override

User approved the design on 2026-09-21 with “开始吧”. This document records that approved design; implementation is authorized.

## Scope and user behavior

Add an optional `合格分数阈值（可选）` editor to each toolchain in AI管理. An empty value means follow the AI response threshold. An explicit finite number from 0 through 1 (including 0) overrides it. Explain beside the editor that higher means stricter and clearing restores the AI threshold. Keep enough decimal precision to avoid rounding saved values merely by selecting and saving a toolchain.

Persist the value in the existing toolchain params JSON as `decision_threshold`, avoiding a database migration. The dedicated editor is the UI authority for that reserved key; show/edit other JSON parameters without a competing editable threshold value. Loading extracts this key; saving inserts it if set and removes it when blank. Preserve other parameters. Existing valid `decision_threshold` parameters populate the editor and become explicit overrides. Missing or null means unset. Malformed, boolean, non-finite, out-of-range values must produce a clear save/preflight error, never silently turn into a pass or corrupt another parameter. Support normal new/load/save/switch/unsaved-change behavior and existing config package round trips.

## Threshold resolution and actual decisions

Resolution order: valid software/request `decision_threshold`, then valid response `ai_result.extra.decision_threshold`, then existing default 0.5. Record the actual chosen threshold and source. Resolve against parameters frozen for the run, not a later edit. Saving applies to subsequent runs without requiring the user to restart the application. Existing historical records are unchanged.

When an override is set and execution succeeds with a valid numeric score in [0,1], software decides pass iff score >= threshold, including equality. This takes precedence over ordinary script PASS/FAIL, OK/NG and Chinese binary labels in both the legacy test session and staged production/import execution path. The raw script result remains available for diagnostics; review/final result must reflect the effective software decision and threshold. Missing/invalid score or failed execution must not become a passing result because of an override. Preserve existing no-override label policy and error behavior, aside from consistent threshold fallback. Existing direct segmentation failure yields score 0 and is treated under the approved score rule; do not add an unrelated hard-rule protocol.

## Implementation boundaries

Reuse existing toolchain storage and request snapshot pipeline. Share threshold resolution logic across execution paths, without introducing broad refactors. Do not modify the external model package, model weights, live database, historical records, or unrelated work. No executable packaging or publishing is requested.

## Compatibility / Migration

- Backward compatibility: partial (intentional priority change for explicitly configured thresholds).
- Protected surfaces: toolchain database schema, params JSON object format, unrelated params, request/response protocol, configuration package import/export, existing unset-threshold behavior, stored history.
- Allowed breakage: explicit software `decision_threshold` now wins over a response threshold and ordinary script decision labels.
- Migration strategy: none; use existing params JSON, and treat absent/null as unset.

## Verification

Exercise UI unset/set/clear/save/reload, precision, switching dirty editors and validation; service persistence/preflight and threshold source/fallback; both execution paths with conflicting labels and thresholds, equality, 0/1, missing/invalid score and failed execution; preservation of unrelated JSON params and configuration package round trip. Run focused regression suites with offscreen Qt and inspect the final diff. Preserve the user's extensive pre-existing working-tree changes using a separate worktree with a recorded snapshot baseline.
