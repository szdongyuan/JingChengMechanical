# Audio Data Result And Config Persistence Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist OK/NG result labels plus config name/config snapshot into `audio_data_table` for newly recorded test runs without modifying data management code.

**Architecture:** Keep the existing early WAV-row creation flow in `TestSession._save_wav()`, but extend it to store config metadata immediately. After automatic judgment completes, update the same audio row using `self._audio_data_id` to backfill the final `labels` value. Add one nullable `config_content` column through startup DB migration and cover both the schema repair and save/update flow with focused tests.

**Tech Stack:** Python, Peewee, SQLite, pytest

---

## File Map

- `model/audio_data_table.py`
  Defines the runtime Peewee schema for `audio_data_table`. Add the new nullable `config_content` field here.
- `app/service/db_connection_service.py`
  Owns startup schema repair. Extend `_migrate()` so legacy/local DBs gain missing audio metadata columns.
- `app/service/analysis/test_session.py`
  Owns WAV row creation and has the stable `self._audio_data_id` handle needed to update the saved row after judgment finishes.
- `tests/test_db_connection_service.py`
  Covers startup bootstrap/migration behavior for `audio_data_table`.
- `tests/test_test_session.py`
  Covers `_save_wav()` row creation and `_run_judgment()` result persistence behavior.

### Task 1: Add Audio Metadata Schema And Initial Row Persistence

**Files:**
- Modify: `model/audio_data_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/analysis/test_session.py`
- Test: `tests/test_db_connection_service.py`
- Test: `tests/test_test_session.py`

- [ ] **Step 1: Write the failing DB bootstrap coverage for `config_content`**

Cover both required schema paths in `tests/test_db_connection_service.py`:

- extend or add a fresh-bootstrap test so a newly created runtime DB includes `audio_data_table.config_content`
- add a legacy migration test that creates `audio_data_table` without `analyse_config` and `config_content`, runs `DbConnectionService.connect()`, and asserts both columns exist afterward

```python
def test_connect_bootstraps_audio_data_table_with_config_content(monkeypatch):
    ...
    db_service.connect()
    bootstrapped_columns = _table_columns(tmp_db, "audio_data_table")
    assert "analyse_config" in bootstrapped_columns
    assert "config_content" in bootstrapped_columns

def test_connect_migrates_audio_data_table_config_content_metadata_columns(monkeypatch):
    ...
    db_service.connect()
    repaired_columns = _table_columns(tmp_db, "audio_data_table")
    assert "analyse_config" in repaired_columns
    assert "config_content" in repaired_columns
```

- [ ] **Step 2: Run the focused DB bootstrap test to verify it fails**

Run: `pytest tests/test_db_connection_service.py -k "audio_data_table and config_content" -q`

Expected: FAIL because fresh bootstrap and/or migration does not yet provide `config_content`.

- [ ] **Step 3: Write the failing `_save_wav()` metadata test**

Extend `tests/test_test_session.py` so `_save_wav()` must persist config metadata when the row is first created.

First update the existing test helper from `def _make_config() -> dict:` to accept keyword overrides so the new tests can express config-name and config-id variants without duplicating the base config:

```python
def _make_config(**overrides) -> dict:
    config = {
        ...
    }
    config.update(overrides)
    return config
```

```python
def test_save_wav_persists_config_name_and_snapshot(tmp_path):
    session = TestSession(
        config=_make_config(config_name="Config A"),
        ...
    )
    path = session._save_wav(buf, sr)
    rec = AudioDataTable.get(AudioDataTable.file_path == path)
    assert rec.analyse_config == "Config A"
    snapshot = json.loads(rec.config_content)
    assert snapshot["config_name"] == "Config A"
    assert snapshot["selected_channels"] == [0]
```

Add focused helper/fallback tests for the config-name chain:

```python
def test_audio_config_metadata_falls_back_to_config_id_when_name_blank(tmp_path):
    session = TestSession(
        config=_make_config(config_id=42, config_name="   "),
        ...
    )
    config_name, _snapshot = session._audio_config_metadata()
    assert config_name == "42"


def test_audio_config_metadata_final_fallback_is_empty_string(tmp_path):
    config = _make_config(config_id=None, config_name="")
    session = TestSession(config=config, ...)
    config_name, _snapshot = session._audio_config_metadata()
    assert config_name == ""
```

Add one snapshot failure test using a circular config reference:

```python
def test_audio_config_metadata_returns_none_snapshot_and_warns_when_json_fails(tmp_path):
    config = _make_config(config_name="Config A")
    config["self"] = config
    bus = _make_mock_bus()
    session = TestSession(config=config, bus=bus, ...)

    config_name, snapshot = session._audio_config_metadata()

    assert config_name == "Config A"
    assert snapshot is None
    assert any(
        "failed to serialize config snapshot" in call.args[0]
        for call in bus.log.emit.call_args_list
    )
```

- [ ] **Step 4: Run the focused `_save_wav()` test to verify it fails**

Run: `pytest tests/test_test_session.py::TestTestSessionWavSave -k "config_metadata or save_wav_persists_config_name" -q`

Expected: FAIL because `_save_wav()` does not yet populate `analyse_config` or `config_content`.

- [ ] **Step 5: Implement the minimal schema and initial save wiring**

Make only these changes:

- in `model/audio_data_table.py`, add:

```python
config_content = TextField(null=True)  # 配置快照
```

- in `app/service/db_connection_service.py`, extend `_migrate()` with:

```python
self._ensure_columns(
    "audio_data_table",
    [
        ("analyse_config", "TEXT"),
        ("config_content", "TEXT"),
    ],
)
```

- in `app/service/analysis/test_session.py`, add a small helper that derives:
  - config name from trimmed `self._config.get("config_name")`
  - fallback to `str(self._config.get("config_id"))` when `config_name` is missing/blank and `config_id` is present
  - final fallback to `""`
  - JSON snapshot from `self._config` via `json.dumps(..., ensure_ascii=False, sort_keys=True, default=str)`

and use it in `_save_wav()`:

```python
AudioDataTable.create(
    ...,
    labels=None,
    barcode=self._workorder.get("sn", ""),
    analyse_config=config_name,
    config_content=config_snapshot,
)
```

If JSON serialization still fails, store `config_content=None` and emit a warning on `self._bus.log`.

- [ ] **Step 6: Re-run the focused schema/save tests**

Run:

```powershell
pytest tests/test_db_connection_service.py -k "audio_data_table and config_content" -q
pytest tests/test_test_session.py::TestTestSessionWavSave -k "config_metadata or save_wav_persists_config_name" -q
```

Expected: PASS.

- [ ] **Step 7: Commit the schema and initial save slice**

```bash
git add model/audio_data_table.py app/service/db_connection_service.py app/service/analysis/test_session.py tests/test_db_connection_service.py tests/test_test_session.py
git commit -m "feat: persist audio config metadata"
```

### Task 2: Backfill Final OK/NG Label After Automatic Judgment

**Files:**
- Modify: `app/service/analysis/test_session.py`
- Test: `tests/test_test_session.py`

- [ ] **Step 1: Write the failing judgment-result persistence tests**

Add focused tests in `tests/test_test_session.py` for both pass and fail result mapping. Keep the tests on `TestSession`, not on data management code.

```python
def test_run_judgment_updates_saved_audio_row_to_ok(tmp_path):
    session = TestSession(...)
    session._judgment = MagicMock()
    session._judgment.judge.return_value = JudgmentResult(
        overall="合格",
        limit_pass=True,
        ai_pass=None,
        ai_score=None,
    )
    session._buffer = buf
    session._sample_rate = sr

    session._run_judgment()

    rec = AudioDataTable.get_by_id(session._audio_data_id)
    assert rec.labels == "OK"
    assert rec.analyse_config == "Config A"
```

```python
def test_run_judgment_updates_saved_audio_row_to_ng(tmp_path):
    ...
    session._judgment.judge.return_value = JudgmentResult(
        overall="不合格",
        limit_pass=False,
        ai_pass=None,
        ai_score=None,
    )
    ...
    assert rec.labels == "NG"
```

Also add one warning-path test that really forces a zero-row update by monkeypatching the DB update query:

```python
def test_run_judgment_keeps_flow_when_audio_row_update_affects_zero_rows(tmp_path):
    ...
    class _ZeroRowQuery:
        def where(self, *_args, **_kwargs):
            return self
        def execute(self):
            return 0

    monkeypatch.setattr(AudioDataTable, "update", lambda **_kwargs: _ZeroRowQuery())
    result = session._run_judgment()
    assert result.overall == "合格"
    assert any(
        "audio metadata update affected 0 rows" in call.args[0]
        for call in bus.log.emit.call_args_list
    )
```

Add one missing-id warning test that calls the metadata persistence helper directly:

```python
def test_persist_audio_result_metadata_warns_and_continues_without_audio_data_id(tmp_path):
    bus = _make_mock_bus()
    session = TestSession(config=_make_config(config_name="Config A"), bus=bus, ...)
    session._audio_data_id = None
    result = JudgmentResult(
        overall="合格",
        limit_pass=True,
        ai_pass=None,
        ai_score=None,
    )

    session._persist_audio_result_metadata(result)

    assert any(
        "audio metadata update skipped: missing audio_data_id" in call.args[0]
        for call in bus.log.emit.call_args_list
    )
```

Add one DB update exception test that verifies result flow remains intact:

```python
def test_run_judgment_keeps_flow_when_audio_row_update_raises(tmp_path, monkeypatch):
    bus = _make_mock_bus()
    session = TestSession(..., bus=bus)
    session._judgment = MagicMock()
    session._judgment.judge.return_value = JudgmentResult(
        overall="合格",
        limit_pass=True,
        ai_pass=None,
        ai_score=None,
    )
    session._buffer = buf
    session._sample_rate = sr

    def _raise_update(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(AudioDataTable, "update", _raise_update)

    result = session._run_judgment()

    assert result.overall == "合格"
    assert any(
        "failed to persist audio result metadata" in call.args[0]
        for call in bus.log.emit.call_args_list
    )
```

Add one unexpected-overall test:

```python
def test_run_judgment_leaves_label_empty_for_unexpected_overall(tmp_path):
    ...
    session._judgment.judge.return_value = JudgmentResult(
        overall="未知",
        limit_pass=True,
        ai_pass=None,
        ai_score=None,
    )

    session._run_judgment()

    rec = AudioDataTable.get_by_id(session._audio_data_id)
    assert rec.labels is None
    assert any(
        "unexpected judgment overall" in call.args[0]
        for call in bus.log.emit.call_args_list
    )
```

- [ ] **Step 2: Run the focused result-persistence tests to verify they fail**

Run: `pytest tests/test_test_session.py -k "updates_saved_audio_row or affects_zero_rows or update_raises or unexpected_overall or missing_audio_data_id" -q`

Expected: FAIL because `_run_judgment()` does not yet update the persisted row.

- [ ] **Step 3: Implement the minimal post-judgment row update**

In `app/service/analysis/test_session.py`, add a helper such as `_persist_audio_result_metadata(result)` that:

- returns early with a warning if `self._audio_data_id` is missing
- maps:
  - `"合格"` -> `"OK"`
  - `"不合格"` -> `"NG"`
  - any other value -> omit `labels` from the update payload and log a warning
- refreshes `analyse_config` / `config_content` from the same config helper used by `_save_wav()`
- builds the update payload conditionally:

```python
update_payload = {
    "analyse_config": config_name,
    "config_content": config_snapshot,
}
if label_value is not None:
    update_payload["labels"] = label_value
```

- updates the row with:

```python
query = (
    AudioDataTable
    .update(**update_payload)
    .where(AudioDataTable.audio_data_id == self._audio_data_id)
)
updated = query.execute()
```

- logs a warning when `updated != 1`
- never raises into the caller; judgment flow must continue

Call this helper after:

```python
result = self._run_ai(result)
self._last_result = result
```

inside `_run_judgment()`.

- [ ] **Step 4: Re-run the focused result-persistence tests**

Run: `pytest tests/test_test_session.py -k "updates_saved_audio_row or affects_zero_rows or update_raises or unexpected_overall or missing_audio_data_id" -q`

Expected: PASS.

- [ ] **Step 5: Run the full focused regression command**

Run: `pytest tests/test_test_session.py tests/test_db_connection_service.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the result-persistence slice**

```bash
git add app/service/analysis/test_session.py tests/test_test_session.py tests/test_db_connection_service.py
git commit -m "feat: persist audio result labels"
```
