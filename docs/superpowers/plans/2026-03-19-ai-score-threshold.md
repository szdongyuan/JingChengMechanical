# AI Score Threshold Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable `score_threshold` to AI bindings so `score >= threshold` becomes the primary AI pass/fail decision, with `ai_label` as fallback.

**Architecture:** Three-layer change: DB model + migration adds the column, AiService CRUD exposes it, TestSession decision logic consumes it. Each layer is independently testable.

**Tech Stack:** Python, peewee ORM, SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-03-19-ai-score-threshold-design.md`

---

## Chunk 1: DB Schema + Migration

### Task 1: Add score_threshold column to model

**Files:**
- Modify: `model/test_config_ai_binding_table.py:10` (import line)
- Modify: `model/test_config_ai_binding_table.py:29` (after `params_json`)

- [ ] **Step 1: Add FloatField import and score_threshold column**

In `model/test_config_ai_binding_table.py`, add `FloatField` to the import and add the column after `params_json`:

```python
# Line 10 — add FloatField to import
from peewee import AutoField, BooleanField, FloatField, ForeignKeyField, IntegerField, TextField

# After line 29 (params_json), add:
    score_threshold = FloatField(null=True, default=None)
```

- [ ] **Step 2: Verify in-memory test DB still creates tables**

Run: `PYTHONPATH=. python -m pytest tests/test_db_connection_service.py::test_connect_creates_ai_tables -v`
Expected: PASS (the autouse fixture in conftest.py creates tables from the model, so `score_threshold` is automatically included for new DBs)

- [ ] **Step 3: Commit**

```bash
git add model/test_config_ai_binding_table.py
git commit -m "feat(model): add score_threshold column to TestConfigAiBindingTable"
```

### Task 2: Add migration for existing databases

**Files:**
- Modify: `app/service/db_connection_service.py:37-54` (`_migrate` method)
- Test: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write failing migration test**

Append to `tests/test_db_connection_service.py`:

```python
def test_migrate_adds_score_threshold_to_existing_binding_table(monkeypatch):
    """connect() must add score_threshold to an existing binding table that lacks it."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        tmp_db = SqliteDatabase(db_path, pragmas={"foreign_keys": 1})
        import model.base_mode as base_mod

        monkeypatch.setattr(base_mod, "db", tmp_db)

        from app.service.db_connection_service import DbConnectionService

        svc = DbConnectionService()
        import app.service.db_connection_service as svc_mod

        monkeypatch.setattr(svc_mod, "db", tmp_db)

        from model.channel_config_table import ChannelConfigTable
        from model.test_config_table import TestConfigTable

        all_models = [
            TestConfigTable,
            ChannelConfigTable,
            AiToolchainTable,
            AiBindingTable,
            AiExecutionTable,
        ]
        tmp_db.bind(all_models)

        # Create a binding table WITHOUT score_threshold (simulating old schema)
        tmp_db.connect()
        tmp_db.execute_sql(
            """
            CREATE TABLE test_config_table (
                config_id INTEGER PRIMARY KEY,
                config_name TEXT NOT NULL UNIQUE,
                record_duration REAL NOT NULL DEFAULT 5.0,
                analysis_duration REAL NOT NULL DEFAULT 3.0,
                scan_triggers_test INTEGER NOT NULL DEFAULT 0,
                created_time TEXT NOT NULL DEFAULT '',
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE ai_toolchain_table (
                toolchain_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '',
                entrypoint TEXT NOT NULL,
                default_timeout_ms INTEGER NOT NULL DEFAULT 30000,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_time TEXT NOT NULL DEFAULT '',
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE test_config_ai_binding_table (
                binding_id INTEGER PRIMARY KEY,
                config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0,
                toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL,
                timeout_ms INTEGER,
                analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0,
                params_json TEXT NOT NULL DEFAULT '{}',
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.close()

        svc.connect()

        cursor = tmp_db.execute_sql("PRAGMA table_info(test_config_ai_binding_table)")
        columns = {row[1]: row for row in cursor.fetchall()}
        assert "score_threshold" in columns
        # Verify default is NULL (dflt_value at index 4)
        assert columns["score_threshold"][4] == "NULL"

        svc.close()
        tmp_db.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/test_db_connection_service.py::test_migrate_adds_score_threshold_to_existing_binding_table -v`
Expected: FAIL — `assert "score_threshold" in column_names`

- [ ] **Step 3: Add migration to _migrate()**

In `app/service/db_connection_service.py`, add after the existing `test_config_table` migration loop (after line 54):

```python
        # AI binding table migrations
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

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/test_db_connection_service.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/service/db_connection_service.py tests/test_db_connection_service.py
git commit -m "feat(migration): add score_threshold column migration for existing DBs"
```

## Chunk 2: AiService CRUD

### Task 3: Add score_threshold to resolve_binding()

**Files:**
- Modify: `app/service/ai_service.py:74-83` (`resolve_binding` return dict)
- Test: `tests/test_ai_service.py`

- [ ] **Step 1: Write failing test for resolve_binding returning score_threshold**

Add to `TestBindingResolver` class in `tests/test_ai_service.py`:

```python
    def test_resolve_binding_includes_score_threshold(self, ai_service, config, toolchain):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True, score_threshold=0.8
        )
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.8

    def test_resolve_binding_score_threshold_defaults_none(self, ai_service, config, toolchain):
        AiBindingTable.create(
            config=config, toolchain=toolchain, enabled=True
        )
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/test_ai_service.py::TestBindingResolver::test_resolve_binding_includes_score_threshold -v`
Expected: FAIL — `KeyError: 'score_threshold'`

- [ ] **Step 3: Add score_threshold to resolve_binding return dict**

In `app/service/ai_service.py`, add to the return dict in `resolve_binding()` (after `"params_json"` line 82):

```python
            "score_threshold": binding.score_threshold,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_ai_service.py::TestBindingResolver -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/service/ai_service.py tests/test_ai_service.py
git commit -m "feat(ai-service): include score_threshold in resolve_binding()"
```

### Task 4: Add score_threshold to save_binding() with validation

**Files:**
- Modify: `app/service/ai_service.py:87-123` (`save_binding` method)
- Test: `tests/test_ai_service.py`

- [ ] **Step 1: Write failing tests for save_binding score_threshold handling**

Add a new class to `tests/test_ai_service.py`:

```python
import math


class TestScoreThresholdCrud:
    """score_threshold validation and persistence in save_binding()."""

    def test_save_binding_round_trip_with_score_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.8

    def test_save_binding_absent_key_preserves_existing_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        # Second save without score_threshold key — must NOT clear it
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] == 0.8

    def test_save_binding_explicit_none_clears_threshold(self, ai_service, config, toolchain):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": 0.8,
        })
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
            "score_threshold": None,
        })
        binding = ai_service.resolve_binding(config.config_id)
        assert binding["score_threshold"] is None

    def test_save_binding_rejects_bool_threshold(self, ai_service, config, toolchain):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": True,
            })

    def test_save_binding_rejects_string_threshold(self, ai_service, config, toolchain):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": "0.8",
            })

    def test_save_binding_rejects_nan_threshold(self, ai_service, config, toolchain):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": float("nan"),
            })

    def test_save_binding_rejects_inf_threshold(self, ai_service, config, toolchain):
        with pytest.raises(ValueError, match="score_threshold"):
            ai_service.save_binding(config.config_id, {
                "enabled": True,
                "score_threshold": float("inf"),
            })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_ai_service.py::TestScoreThresholdCrud -v`
Expected: Multiple failures

- [ ] **Step 3: Implement save_binding score_threshold logic**

In `app/service/ai_service.py`, add `import math` at the top (after line 9, with the other stdlib imports).

Then in `save_binding()`, add validation after the `analysis_window_offset_ms` check (after line 94):

```python
        if "score_threshold" in data:
            st = data["score_threshold"]
            if st is not None:
                if isinstance(st, bool) or not isinstance(st, (int, float)):
                    raise ValueError("score_threshold must be numeric (int or float, not bool)")
                if math.isnan(st) or math.isinf(st):
                    raise ValueError("score_threshold must be finite (not NaN or inf)")
```

In the **update** path (existing binding, around lines 101-110), add before `existing.save()`:

```python
                if "score_threshold" in data:
                    existing.score_threshold = data["score_threshold"]
```

In the **create** path (around lines 113-122), add `score_threshold` to the `create()` call:

```python
                row = TestConfigAiBindingTable.create(
                    config=config_id,
                    enabled=data.get("enabled", False),
                    toolchain=data.get("toolchain_id"),
                    timeout_ms=data.get("timeout_ms"),
                    analysis_window_offset_ms=data.get(
                        "analysis_window_offset_ms", 0
                    ),
                    params_json=data.get("params_json", "{}"),
                    score_threshold=data.get("score_threshold"),
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_ai_service.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/service/ai_service.py tests/test_ai_service.py
git commit -m "feat(ai-service): save_binding() persists and validates score_threshold"
```

## Chunk 3: TestSession Decision Logic

### Task 5: Replace ai_label-only logic with score threshold decision

**Files:**
- Modify: `app/service/analysis/test_session.py:168-249` (`_run_ai` method)
- Test: `tests/test_test_session.py`

- [ ] **Step 1: Write failing tests for score threshold decision logic**

Add a new class to `tests/test_test_session.py`. These tests call `_run_ai()` directly with mocked AI service to test the decision logic in isolation.

Add `import math` at the top of the test file.

```python
class TestScoreThresholdDecision:
    """_run_ai() score threshold decision logic."""

    def _make_session(self, tmp_path, ai_mock, binding):
        ai_mock.resolve_binding.return_value = binding
        session = TestSession(
            config=_make_config(), bus=_make_mock_bus(), ai_service=ai_mock,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "test.wav")
        session._audio_data_id = "audio-test"
        return session

    def _make_base_result(self):
        return JudgmentResult(
            overall="合格", limit_pass=True, ai_pass=None, ai_score=None,
        )

    def test_score_above_threshold(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": 0.9, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.overall == "合格"

    def test_score_below_threshold_overrides_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": 0.3, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.overall == "不合格"

    def test_score_at_threshold_passes(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "FAIL",
            "score": 0.8, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.overall == "合格"

    def test_score_present_no_threshold_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": 0.9, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": None,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True

    def test_no_score_threshold_set_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "FAIL",
            "score": None, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False

    def test_no_score_no_threshold_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": None, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": None,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True

    def test_nan_score_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": float("nan"), "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True

    def test_inf_score_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "FAIL",
            "score": float("inf"), "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/test_test_session.py::TestScoreThresholdDecision -v`
Expected: Multiple failures — current `_run_ai()` ignores `score_threshold`

- [ ] **Step 3: Implement the score threshold decision logic**

In `app/service/analysis/test_session.py`, add `import math` after the existing imports (after line 5, with other stdlib imports).

Replace the decision block in `_run_ai()` (lines 237-244) with:

```python
        score = envelope.get("score")
        ai_label = envelope.get("ai_label")
        score_threshold = binding.get("score_threshold")

        # Guard: treat NaN/inf score as "no score"
        if score is not None and (math.isnan(score) or math.isinf(score)):
            score = None

        # Primary: score + threshold
        ai_pass = None
        if score is not None and score_threshold is not None:
            ai_pass = (score >= score_threshold)
        # Fallback: ai_label (legacy toolchains without numeric score)
        elif isinstance(ai_label, str):
            label = ai_label.strip().upper()
            if label == "PASS":
                ai_pass = True
            elif label == "FAIL":
                ai_pass = False
```

Leave lines 246-249 (result patching and overall verdict) unchanged — they already consume `ai_pass` correctly.

- [ ] **Step 4: Run all tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/test_test_session.py -v`
Expected: All PASS (new score threshold tests + existing tests that still use label fallback)

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_session.py tests/test_test_session.py
git commit -m "feat(test-session): score threshold decision logic in _run_ai()"
```

### Task 6: Full verification

- [ ] **Step 1: Run entire test suite**

Run: `PYTHONPATH=. python -m pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Commit (if any fixups needed)**

Only if corrections were needed in the previous step.
