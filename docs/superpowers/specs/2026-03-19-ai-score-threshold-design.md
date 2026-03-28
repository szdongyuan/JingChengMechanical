# AI Score Threshold Design

## Context

The analysis kernel's `TestSession._run_ai()` currently determines AI pass/fail solely from the `ai_label` string ("PASS"/"FAIL") returned by the AI toolchain. The numeric `score` field is stored but never used in decision-making. For a binary classification model, the model outputs a probability score and the threshold should determine pass/fail — `ai_label` is redundant when a score is available.

Spec R3 requires: "Pass/fail = SPL/FFT/octave band limit checks AND AI score combined." This design closes that gap by adding a configurable `score_threshold` to the AI binding, making `score >= threshold` the primary decision mechanism.

## Design

### DB Schema

Add `score_threshold` column to `TestConfigAiBindingTable`:

```python
score_threshold = FloatField(null=True, default=None)
```

- `None` → no threshold configured, fall back to `ai_label` string
- Any finite float (e.g. `0.5`, `0.8`) → use `score >= threshold` as primary pass/fail

**File:** `model/test_config_ai_binding_table.py`

### Migration

The codebase uses additive `ALTER TABLE` migrations in `DbConnectionService._migrate()` (`app/service/db_connection_service.py`). Add a new entry for the `test_config_ai_binding_table`:

```python
# In _migrate(), add after existing test_config_table migrations:
binding_columns = [
    ("score_threshold", "REAL DEFAULT NULL"),
]
for column_name, type_spec in binding_columns:
    try:
        db.execute_sql(
            f"ALTER TABLE test_config_ai_binding_table ADD COLUMN {column_name} {type_spec}"
        )
    except OperationalError:
        pass
```

This ensures existing databases get the new column on next startup.

**File:** `app/service/db_connection_service.py`

### AiService Changes

**`resolve_binding()`** — include `score_threshold` in the returned dict:

```python
return {
    ...,
    "score_threshold": binding.score_threshold,
}
```

**`save_binding()`** — persist `score_threshold` from input data:

- If `"score_threshold"` key is **absent** from `data`, preserve the existing DB value (do not overwrite with `None`). This is critical because the UI does not yet expose this field — ordinary config saves must not clear a threshold set via other means (API, direct DB edit).
- If present and not `None`, validate: must be numeric (`int` or `float`), not `bool`, not `NaN`, not `±inf`. Raise `ValueError` on invalid input.
- If present and `None`, clear the threshold (revert to label-only behavior).

**File:** `app/service/ai_service.py`

### TestSession._run_ai() Decision Logic

After receiving a successful AI envelope, apply this decision order:

```python
import math

score = envelope.get("score")
ai_label = envelope.get("ai_label")
score_threshold = binding.get("score_threshold")

# Guard: treat NaN/inf score as "no score"
if score is not None and (math.isnan(score) or math.isinf(score)):
    score = None

# Primary: score + threshold
if score is not None and score_threshold is not None:
    ai_pass = (score >= score_threshold)
# Fallback: ai_label (legacy toolchains without numeric score)
elif isinstance(ai_label, str):
    label = ai_label.strip().upper()
    if label == "PASS":
        ai_pass = True
    elif label == "FAIL":
        ai_pass = False
    else:
        ai_pass = None
else:
    ai_pass = None
```

The overall verdict rule is unchanged: `limit_pass AND (ai_pass is True or ai_pass is None)`.

**File:** `app/service/analysis/test_session.py`

### Test Coverage

#### Decision logic tests (`tests/test_test_session.py`)

| Case | score | threshold | ai_label | Expected ai_pass |
|------|-------|-----------|----------|-----------------|
| Score above threshold | 0.9 | 0.8 | "PASS" | True (from score) |
| Score below threshold | 0.3 | 0.8 | "PASS" | False (score overrides label) |
| Score at threshold | 0.8 | 0.8 | "FAIL" | True (score overrides label) |
| Score present, no threshold | 0.9 | None | "PASS" | True (from label fallback) |
| No score, threshold set | None | 0.8 | "FAIL" | False (from label fallback) |
| No score, no threshold | None | None | "PASS" | True (from label fallback) |
| NaN score with threshold | NaN | 0.8 | "PASS" | True (NaN → fallback to label) |
| Inf score with threshold | inf | 0.8 | "FAIL" | False (inf → fallback to label) |

#### Service-layer tests (`tests/test_ai_service.py`)

| Case | Expected |
|------|----------|
| Binding save/resolve round-trip with score_threshold=0.8 | Field persists correctly |
| Binding save without score_threshold key preserves existing value | Existing 0.8 not cleared |
| Binding save with score_threshold=None clears value | Field becomes None |
| save_binding rejects score_threshold=True | ValueError |
| save_binding rejects score_threshold="0.8" | ValueError |
| save_binding rejects score_threshold=NaN | ValueError |
| save_binding rejects score_threshold=inf | ValueError |

#### Migration test (`tests/test_db_connection_service.py`)

| Case | Expected |
|------|----------|
| Existing DB without score_threshold column gains it after connect() | Column exists with NULL default |

### Edge Cases

- **NaN score:** Treat as "no score" — fall through to `ai_label` fallback. Guard: `score is not None and not math.isnan(score) and not math.isinf(score)`.
- **NaN/inf threshold:** Rejected at `save_binding()` validation — never persisted.
- **Negative threshold:** Allowed — some scoring systems use negative-to-positive ranges. No artificial constraint.
- **Score exactly at threshold:** `>=` means it passes. This is documented in the test matrix.

### UI Integration (Deferred)

The config UI (`ui/modules/config.py`) has `_populate_ai_binding()` and `_collect_ai_binding()` that manage AI binding fields. A `score_threshold` spin box will be added when the analysis config UI is built. Until then, the field defaults to `None` (preserving current label-only behavior).

**Compatibility note:** Because `_collect_ai_binding()` does not include `score_threshold` in the returned dict, `save_binding()` must preserve the existing DB value when the key is absent. This is specified in the `save_binding()` section above.

### Files Modified

1. `model/test_config_ai_binding_table.py` — add `score_threshold` column
2. `app/service/db_connection_service.py` — add migration for `score_threshold` column
3. `app/service/ai_service.py` — `resolve_binding()`, `save_binding()` include score_threshold
4. `app/service/analysis/test_session.py` — `_run_ai()` decision logic
5. `tests/test_test_session.py` — score threshold decision logic test cases
6. `tests/test_ai_service.py` — binding CRUD round-trip and validation tests
7. `tests/test_db_connection_service.py` — migration test for score_threshold column

### Verification

```bash
# Run targeted tests
PYTHONPATH=. python -m pytest tests/test_test_session.py tests/test_ai_service.py tests/test_db_connection_service.py -v --tb=short

# Run full analysis kernel suite
PYTHONPATH=. python -m pytest tests/ -q --tb=short
```
