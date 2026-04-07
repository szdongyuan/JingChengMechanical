# Remove `score_threshold` Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `score_threshold` from the app and make AI pass/fail and deviation interpretation use one effective threshold resolved from toolchain output, request params, or default `0.5`.

**Architecture:** Remove binding-level threshold state from the persistence layer, then centralize effective threshold resolution in `AiService.run_ai_analysis` for successful runs. `TestSession` and summary payload builders consume the resolved threshold from the runtime envelope so the final verdict and displayed AI deviation stay consistent.

**Tech Stack:** Python, Peewee ORM, SQLite, Pytest, PyQt6

---

## File Map

- Modify: `app/service/ai_service.py`
  - Remove `score_threshold` from binding CRUD and binding resolution.
  - Resolve one effective `decision_threshold` into successful AI execution envelopes.
  - Reject legacy `score_threshold` writes.
- Modify: `app/service/analysis/test_session.py`
  - Stop reading binding `score_threshold`.
  - Use runtime envelope `decision_threshold` for numeric-score pass/fail.
  - Tighten fallback behavior for score-less / unknown-label AI results.
- Modify: `app/service/analysis/review_payloads.py`
  - Remove the “successful AI with no binding threshold becomes unconstrained OK” path.
  - Compute AI deviation from the resolved threshold carried in `ai_review`.
- Modify: `app/service/db_connection_service.py`
  - Stop adding `score_threshold` during bootstrap.
  - Destructively remove the column from legacy `test_config_ai_binding_table` on startup while preserving remaining data.
- Modify: `model/test_config_ai_binding_table.py`
  - Remove the Peewee field for `score_threshold`.
- Modify: `tests/test_ai_service.py`
  - Remove score-threshold binding tests.
  - Add tests for binding rejection when callers still send `score_threshold`.
- Modify: `tests/test_ai_service_integration.py`
  - Add integration tests for runtime threshold resolution priority and fallback.
- Modify: `tests/test_test_session.py`
  - Rewrite AI-threshold decision tests to use resolved runtime `decision_threshold`.
  - Add unknown-`ai_label` and default-`0.5` fallback cases.
- Modify: `tests/test_review_payloads.py`
  - Add AI summary tests proving numeric success with resolved threshold yields deviation and participates in overall status without any binding threshold.
- Modify: `tests/test_db_connection_service.py`
  - Replace legacy assertions that expect `score_threshold` to exist with assertions that it is removed.
- Modify: `tests/test_ai_tool_management_module.py`
  - Remove obsolete fake binding payload keys that mention `score_threshold` where they no longer matter to preserved binding state.

## Task 1: Remove `score_threshold` From Binding Model And Service Contract

**Files:**
- Modify: `model/test_config_ai_binding_table.py`
- Modify: `app/service/ai_service.py`
- Test: `tests/test_ai_service.py`

- [ ] **Step 1: Write failing service-contract tests for the removed binding field**

Add or replace tests in `tests/test_ai_service.py` so they describe the new contract:

```python
def test_resolve_binding_does_not_include_score_threshold(ai_service, config, toolchain):
    AiBindingTable.create(config=config, toolchain=toolchain, enabled=True)
    binding = ai_service.resolve_binding(config.config_id)
    assert "score_threshold" not in binding


def test_save_binding_rejects_removed_score_threshold(ai_service, config):
    with pytest.raises(ValueError, match="score_threshold"):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "score_threshold": 0.5,
        })
```

- [ ] **Step 2: Run the focused service tests to verify they fail**

Run: `pytest tests/test_ai_service.py -k "score_threshold or resolve_binding" -v`

Expected: FAIL because the current service still reads, writes, and returns `score_threshold`.

- [ ] **Step 3: Remove the field from the model and service binding API**

Implement the minimal contract change:

```python
# model/test_config_ai_binding_table.py
class TestConfigAiBindingTable(BaseModel):
    ...
    params_json = TextField(default="{}")
    updated_time = TextField(default=now_str)
```

```python
# app/service/ai_service.py
def resolve_binding(self, config_id: int) -> dict | None:
    ...
    return {
        "binding_id": binding.binding_id,
        "config_id": binding.config_id,
        "enabled": bool(binding.enabled),
        "toolchain_id": tc_id,
        "timeout_ms": binding.timeout_ms,
        "effective_timeout_ms": effective_timeout,
        "params_json": binding.params_json,
    }


def save_binding(self, config_id: int, data: dict) -> int:
    if "score_threshold" in data:
        raise ValueError("score_threshold has been removed")
```

Also remove `score_threshold` from create/update SQL paths in this file.

- [ ] **Step 4: Run the focused service tests to verify they pass**

Run: `pytest tests/test_ai_service.py -k "score_threshold or resolve_binding" -v`

Expected: PASS with no remaining binding API assertions that depend on `score_threshold`.

- [ ] **Step 5: Commit**

```bash
git add model/test_config_ai_binding_table.py app/service/ai_service.py tests/test_ai_service.py
git commit -m "refactor: remove score-threshold binding contract"
```

## Task 2: Centralize Effective Threshold Resolution In `AiService`

**Files:**
- Modify: `app/service/ai_service.py`
- Test: `tests/test_ai_service_integration.py`

- [ ] **Step 1: Write failing integration tests for threshold resolution priority**

Add integration coverage in `tests/test_ai_service_integration.py` using fake toolchain scripts that emit different `extra.decision_threshold` payloads:

```python
def test_run_ai_analysis_uses_response_threshold_when_present(...):
    assert result["decision_threshold"] == 0.8


def test_run_ai_analysis_falls_back_to_request_threshold_when_response_threshold_invalid(...):
    assert result["decision_threshold"] == 0.6


def test_run_ai_analysis_falls_back_to_default_point_five_when_no_valid_threshold_exists(...):
    assert result["decision_threshold"] == 0.5
```

Also add a case where the response threshold is missing and request `decision_threshold` is invalid, proving default `0.5`.

- [ ] **Step 2: Run the focused integration tests to verify they fail**

Run: `pytest tests/test_ai_service_integration.py -k "decision_threshold or threshold" -v`

Expected: FAIL because the current execution envelope does not expose resolved `decision_threshold`.

- [ ] **Step 3: Implement the envelope-level threshold resolver**

Add small helpers in `app/service/ai_service.py` so successful runs always carry one resolved threshold:

```python
def _coerce_probability(value):
    ...


def _resolve_effective_decision_threshold(ai_result: dict, request_params: dict) -> tuple[float, str]:
    response_threshold = _coerce_probability(ai_result.get("extra", {}).get("decision_threshold"))
    if response_threshold is not None:
        return response_threshold, "response_extra"
    request_threshold = _coerce_probability(request_params.get("decision_threshold"))
    if request_threshold is not None:
        return request_threshold, "request_params"
    return 0.5, "default"
```

In `run_ai_analysis`, after successful validation:

```python
decision_threshold, threshold_source = _resolve_effective_decision_threshold(
    ai_result,
    params,
)
envelope["decision_threshold"] = decision_threshold
envelope["decision_threshold_source"] = threshold_source
```

Keep this resolution inside `AiService`; downstream code should not reimplement the fallback chain.

- [ ] **Step 4: Run the focused integration tests to verify they pass**

Run: `pytest tests/test_ai_service_integration.py -k "decision_threshold or threshold" -v`

Expected: PASS with envelope-level threshold resolution covering response, request, and default fallback.

- [ ] **Step 5: Commit**

```bash
git add app/service/ai_service.py tests/test_ai_service_integration.py
git commit -m "feat: resolve ai decision threshold in runtime envelope"
```

## Task 3: Consume The Resolved Threshold In `TestSession`

**Files:**
- Modify: `app/service/analysis/test_session.py`
- Test: `tests/test_test_session.py`

- [ ] **Step 1: Write failing `TestSession` tests for resolved-threshold consumption**

Rewrite or replace the score-threshold tests in `tests/test_test_session.py` so they use runtime envelope `decision_threshold` instead of binding `score_threshold`:

```python
def test_score_below_envelope_threshold_fails_even_if_ai_label_is_pass(tmp_path):
    ai.run_ai_analysis.return_value = {
        "module_status": "success",
        "ai_label": "PASS",
        "score": 0.3,
        "decision_threshold": 0.8,
        "error_summary": None,
    }
    session = self._make_session(tmp_path, ai, {"enabled": True})
    result = session._run_ai(self._make_base_result())
    assert result.ai_pass is False
```

Add cases for:

- missing response threshold + valid request `decision_threshold` => pass/fail uses request value
- missing/invalid thresholds => default `0.5`
- usable numeric `score` always takes priority over `ai_label`
- score-less success + `ai_label == "FAIL"` => fallback false
- score-less success + `ai_label == "PASS"` => fallback true
- score-less success + unknown `ai_label` => degrade to `无结果`-style review instead of silent pass
- score-less success + missing / non-string / otherwise unusable `ai_label` => degrade to `无结果` with an explicit error message
- non-finite / non-numeric `score` values are treated as unusable score states and therefore enter the `ai_label` fallback path

- [ ] **Step 2: Run the focused session tests to verify they fail**

Run: `pytest tests/test_test_session.py -k "threshold or ai_label or missing_score" -v`

Expected: FAIL because the current session code still reads binding `score_threshold`.

- [ ] **Step 3: Implement the session-side runtime consumption changes**

Update `app/service/analysis/test_session.py`:

```python
effective_threshold = self._coerce_ai_score(envelope.get("decision_threshold"))
score = self._coerce_ai_score(envelope.get("score"))
# From here on, `score is None` means there is no usable numeric score.

if score is not None and effective_threshold is not None:
    ai_pass = (score >= effective_threshold)
elif score is None and isinstance(ai_label, str):
    label = ai_label.strip().upper()
    if label == "PASS":
        ai_pass = True
    elif label == "FAIL":
        ai_pass = False
    else:
        ai_pass = None
        ai_review_status = "missing_score"
        ai_review_error = "AI label is not a recognized binary label"
elif score is None:
    ai_pass = None
    ai_review_status = "missing_score"
    ai_review_error = "AI score and usable AI label are unavailable"
```

Also:

- stop reading `binding.get("score_threshold")`
- store `effective_threshold` into `last_ai_review["threshold"]` for successful validated runs
- treat `score is not None` with missing envelope threshold as a defensive no-result path with an explicit error instead of falling back to `ai_label`
- preserve current failure-path `threshold=None` behavior for non-success runs that do not provide an envelope threshold

- [ ] **Step 4: Run the focused session tests to verify they pass**

Run: `pytest tests/test_test_session.py -k "threshold or ai_label or missing_score" -v`

Expected: PASS with final verdicts driven by runtime threshold resolution rather than binding state.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_session.py tests/test_test_session.py
git commit -m "refactor: consume resolved ai thresholds in test session"
```

## Task 4: Make The Summary Payload Consume Numeric AI Results Consistently

**Files:**
- Modify: `app/service/analysis/review_payloads.py`
- Test: `tests/test_review_payloads.py`

- [ ] **Step 1: Write failing summary tests for numeric AI without binding threshold**

Add focused tests in `tests/test_review_payloads.py`:

```python
def test_build_summary_payload_uses_ai_threshold_for_numeric_success():
    summary = build_summary_payload(
        config=_config(),
        judgment_payload=_judgment_payload(),
        ai_review={"status": "success", "threshold": 0.5, "score": 0.48},
    )
    assert summary["rows"][-1]["deviation_display"] == "0.02"
    assert summary["rows"][-1]["status"] == "NG"
```

Also add a case where `status == "success"` and `threshold` is present but `score` is missing, proving AI row becomes `无结果`, and a case where disabled/not-configured AI remains unconstrained.

Also add a case where:

- `judgment_payload["ai_pass"]` is already `True` because `TestSession` fell back to `ai_label == "PASS"`
- `ai_review["status"] == "success"`
- `ai_review["score"] is None`
- `ai_review["threshold"]` is present

and assert the summary still renders:

- AI row `status == "无结果"`
- AI row `deviation_display == "--"`

This locks down the spec requirement that score-less label fallback may affect final judgment but must not create synthetic summary deviation or summary `OK`/`NG`.

- [ ] **Step 2: Run the focused summary tests to verify they fail**

Run: `pytest tests/test_review_payloads.py -k "AI分析 or ai_threshold or unconstrained" -v`

Expected: FAIL because the current AI summary row still treats successful AI with missing binding threshold as unconstrained.

- [ ] **Step 3: Implement the AI summary row behavior**

Update `app/service/analysis/review_payloads.py` so successful numeric AI uses `threshold` directly:

```python
if status == "success" and score is not None and threshold is not None:
    deviation = _compute_deviation(actual=score, lower=threshold, upper=None)
    return _build_row(...), None

if status == "success" and score is not None and threshold is None:
    return _build_constrained_no_result_row("ai", "AI分析", channel_index=channel_index), \
        "AI decision threshold unavailable"
```

Keep disabled / not-configured / skipped states unconstrained, and keep score-less success as no-result rather than synthetic deviation.

- [ ] **Step 4: Run the focused summary tests to verify they pass**

Run: `pytest tests/test_review_payloads.py -k "AI分析 or ai_threshold or unconstrained" -v`

Expected: PASS with AI deviation and row status aligned to the resolved runtime threshold.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/review_payloads.py tests/test_review_payloads.py
git commit -m "fix: align ai summary deviation with runtime threshold"
```

## Task 5: Remove `score_threshold` From Runtime Schema Bootstrap

**Files:**
- Modify: `app/service/db_connection_service.py`
- Modify: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write failing DB bootstrap tests for destructive column removal**

Replace the legacy expectations in `tests/test_db_connection_service.py` with tests that require startup cleanup:

```python
def test_connect_removes_score_threshold_from_legacy_ai_binding_table(monkeypatch):
    ...
    svc.connect()
    columns = _table_columns(tmp_db, "test_config_ai_binding_table")
    assert "score_threshold" not in columns
    assert "analysis_window_offset_ms" in columns
```

Also assert that remaining data survives the rebuild:

```python
assert row == (1, "legacy-toolchain", 1200, 250, '{"legacy": true}')
```

- [ ] **Step 2: Run the focused DB bootstrap tests to verify they fail**

Run: `pytest tests/test_db_connection_service.py -k "score_threshold or ai_binding_table" -v`

Expected: FAIL because bootstrap currently adds and preserves `score_threshold`.

- [ ] **Step 3: Implement destructive startup cleanup**

Add a helper in `app/service/db_connection_service.py` that rebuilds `test_config_ai_binding_table` without `score_threshold` when the column exists:

```python
def _remove_obsolete_ai_binding_score_threshold(self):
    columns = self._get_table_columns("test_config_ai_binding_table")
    if "score_threshold" not in columns:
        return
    db.execute_sql("ALTER TABLE test_config_ai_binding_table RENAME TO test_config_ai_binding_table_old")
    db.execute_sql("""CREATE TABLE test_config_ai_binding_table (...)""")
    db.execute_sql("""INSERT INTO test_config_ai_binding_table (...) SELECT ... FROM test_config_ai_binding_table_old""")
    db.execute_sql("DROP TABLE test_config_ai_binding_table_old")
    self._ensure_ai_binding_indexes()
```

Call this from `_migrate()` and remove the old `_ensure_columns(... score_threshold ...)` bootstrap path.

- [ ] **Step 4: Run the focused DB bootstrap tests to verify they pass**

Run: `pytest tests/test_db_connection_service.py -k "score_threshold or ai_binding_table" -v`

Expected: PASS with startup removing obsolete `score_threshold` while preserving all other binding data.

- [ ] **Step 5: Commit**

```bash
git add app/service/db_connection_service.py tests/test_db_connection_service.py
git commit -m "refactor: drop score-threshold from runtime schema"
```

## Task 6: Run Full Regression Sweep And Clean Up Dependent Fixtures

**Files:**
- Modify: `tests/test_ai_tool_management_module.py`
- Modify: any additional touched tests from previous tasks

- [ ] **Step 1: Remove stale test fixture payload keys and update any dependent assertions**

Clean up fake binding rows and helper assertions that still mention `score_threshold` solely because of the old binding shape:

```python
{
    "config_id": 1,
    "config_name": "Cfg One",
    "toolchain_id": None,
    "enabled": False,
    "timeout_ms": 3000,
    "params_json": '{"mode":"a"}',
}
```

- [ ] **Step 2: Run the full targeted regression suite**

Run:

```bash
pytest tests/test_ai_service.py tests/test_ai_service_integration.py tests/test_test_session.py tests/test_review_payloads.py tests/test_db_connection_service.py tests/test_ai_tool_management_module.py -v
```

Expected: PASS

- [ ] **Step 3: Run a final repo-wide smoke subset if the targeted suite passes**

Run:

```bash
pytest tests/test_result_module.py tests/test_test_manager/test_review_payload_emission.py -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_ai_tool_management_module.py tests/test_result_module.py tests/test_test_manager/test_review_payload_emission.py
git commit -m "test: update ai threshold regressions"
```
