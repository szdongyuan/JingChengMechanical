# Normal Distribution Report Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an in-app historical normal-distribution/statistics report for saved measurement records, backed by a derived metric cache table and HTML/CSV export.

**Architecture:** Add a Peewee cache model and keep all historical metric computation, cache validation, aggregation, and export logic in focused service modules under `app/service/`. The service reuses existing data filters, WAV normalization, `JudgmentEngine`, chart payload builders, and current microphone calibration; the UI stays native PyQt/pyqtgraph and calls the service through `ServiceContainer`.

**Tech Stack:** Python 3, Peewee/SQLite, NumPy/SciPy WAV analysis, PyQt6, pyqtgraph, pytest.

---

## Source Spec

- `docs/double-sdd/specs/2026-06-18-normal-distribution-report-design.md`

## File Structure

- Create `model/historical_metric_cache_table.py`
  - Peewee model for `historical_metric_cache_table`.
  - Owns field names, nullability, indexes, and unique key.
- Create `app/service/historical_metric_report_types.py`
  - Shared constants, metric definitions, exceptions, and dataclasses only.
- Create `app/service/historical_metric_report_cache.py`
  - Cache signatures, validity checks, record-scoped refresh, WAV loading, current calibration hash, and metric extraction.
  - Reuses `load_wav()`, `selected_config_channels()`, `JudgmentEngine`, `build_chart_payload()`, and current `hss_service.load_io_devices()["mic_cal"]`.
- Create `app/service/historical_metric_report_statistics.py`
  - Grouping, sample statistics, histogram/normal-curve data, and outlier detection.
- Create `app/service/historical_metric_report_service.py`
  - Report filters, progress/cancel handling, orchestration, current-row metadata joins, summary counters, and final `HistoricalMetricReportResult` assembly.
  - Reuses `AudioDataService._filtered_query()`, `HistoricalMetricCacheComputer`, and `historical_metric_report_statistics`.
- Create `app/service/historical_metric_report_export.py`
  - Static HTML and UTF-8 BOM CSV detail export for a completed report result.
- Create `ui/popups/historical_metric_report_dialog.py`
  - Native PyQt report dialog, background worker thread, filters, summary counters, statistics table, histogram/normal-curve plot, failure table, outlier table, and export controls.
- Modify `app/service/db_connection_service.py`
  - Require the new prepared runtime table and verify required columns/index assumptions without auto-migrating existing databases.
- Modify `app/services_container.py`
  - Register `historical_metric_report_service` with a lazy import inside `ServiceContainer.__post_init__`.
  - Do not add a top-level `HistoricalMetricReportService` import here; `historical_metric_report_cache.py` imports `app.service.analysis.imported_audio`, and that module imports shared dataclasses from `app.services_container`.
- Modify `ui/modules/data_management.py`
  - Add the `统计报告` toolbar action and open the report dialog without requiring row selection.
- Modify `tests/conftest.py`
  - Bind/create/drop the new model in the shared in-memory test DB.
- Modify `tests/test_db_connection_service.py`
  - Add required-table and prepared-schema coverage for the cache table.
- Modify `tests/test_data_management_module.py`
  - Bring the existing stub up to the current pagination API and cover the new toolbar action.
- Create `tests/test_historical_metric_cache_table.py`
  - Model and SQLite schema checks.
- Create `tests/test_historical_metric_report_service.py`
  - Filter reuse, cache validity, recomputation, failure rows, metric extraction, aggregation, outliers, and calibration invalidation.
- Create `tests/test_historical_metric_report_export.py`
  - HTML/CSV export checks.
- Create `tests/test_historical_metric_report_dialog.py`
  - Native UI and worker-thread behavior with service stubs.
- Optional create `tools/prepare_historical_metric_cache_table.py`
  - Explicit operator/developer utility for adding the cache table to an existing prepared `jingchen.db`. Do not call it automatically from app startup.

## Cross-Cutting Rules

- Do not change live analysis algorithms, OK/NG semantics, existing audio ZIP export, or historical `audio_data_table` rows.
- Do not auto-create or auto-migrate the runtime database at app startup. The startup path stays "prepared database required".
- Cache refresh is record-scoped: delete all cache rows for the `audio_data_id`, then insert the fresh current rows inside one transaction.
- Report filtering and display must join against current `audio_data_table`; cache snapshots are audit/debug only.
- Historical WAV recomputation supports only projected runtime WAVs where `wav.channel_count == len(selected_channels)`.
- A one-column WAV with historical `selected_channels=[3]` maps config channel `3` to WAV column `0`.
- A WAV with fewer or more columns than `len(selected_channels)` is a record-level failure; do not guess full-hardware layouts.
- Historical report windows use native PyQt widgets and pyqtgraph plots. Do not embed HTML in the app.
- HTML export must be static and self-contained, with no network resources.
- CSV export uses `encoding="utf-8-sig"`.
- Keep report implementation split by responsibility. Do not move cache recomputation, statistics/outlier math, export rendering, and dialog code into `HistoricalMetricReportService`; that class should remain an orchestrator.

---

### Task 1: Cache Table Model And Startup Schema

**Files:**
- Create: `model/historical_metric_cache_table.py`
- Create: `tests/test_historical_metric_cache_table.py`
- Modify: `tests/conftest.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `tests/test_db_connection_service.py`
- Optional create: `tools/prepare_historical_metric_cache_table.py`

- [ ] **Step 1: Write failing model/schema tests**

Create `tests/test_historical_metric_cache_table.py` with schema assertions:

```python
from model.historical_metric_cache_table import HistoricalMetricCacheTable


def test_historical_metric_cache_table_has_required_fields(setup_test_db):
    columns = set(HistoricalMetricCacheTable._meta.fields)

    assert {
        "cache_id",
        "audio_data_id",
        "file_path",
        "file_size",
        "file_mtime_ns",
        "record_date",
        "product_model",
        "barcode",
        "label",
        "label_source",
        "analyse_config",
        "config_hash",
        "calibration_hash",
        "algorithm_version",
        "channel_index",
        "channel_label",
        "metric_key",
        "metric_label",
        "metric_value",
        "metric_unit",
        "status",
        "error_message",
        "computed_at",
    } <= columns


def test_historical_metric_cache_table_sqlite_types_and_nullability(setup_test_db):
    cursor = setup_test_db.execute_sql(
        "PRAGMA table_info(historical_metric_cache_table)"
    )
    columns = {
        row[1]: {"type": row[2].upper(), "notnull": bool(row[3]), "pk": bool(row[5])}
        for row in cursor.fetchall()
    }

    assert columns["cache_id"] == {"type": "INTEGER", "notnull": True, "pk": True}
    assert columns["audio_data_id"] == {"type": "TEXT", "notnull": True, "pk": False}
    assert columns["file_path"] == {"type": "TEXT", "notnull": True, "pk": False}
    assert columns["file_size"] == {"type": "INTEGER", "notnull": True, "pk": False}
    assert columns["file_mtime_ns"] == {"type": "INTEGER", "notnull": True, "pk": False}
    assert columns["metric_value"] == {"type": "REAL", "notnull": False, "pk": False}
    assert columns["error_message"] == {"type": "TEXT", "notnull": False, "pk": False}
    assert columns["computed_at"] == {"type": "TEXT", "notnull": True, "pk": False}


def test_historical_metric_cache_unique_key_and_indexes(setup_test_db):
    cursor = setup_test_db.execute_sql(
        "PRAGMA index_list(historical_metric_cache_table)"
    )
    index_names = {row[1] for row in cursor.fetchall()}

    assert any("audio_data_id" in name for name in index_names)
    assert any("metric_key" in name for name in index_names)

    HistoricalMetricCacheTable.create(
        audio_data_id="a1",
        file_path="x.wav",
        file_size=10,
        file_mtime_ns=20,
        record_date="2026-06-18T10:00:00+08:00",
        product_model="M1",
        config_hash="cfg",
        calibration_hash="cal",
        algorithm_version="2026-06-18.1",
        channel_index=0,
        channel_label="CH1",
        metric_key="spl_peak_db",
        metric_label="声压级峰值",
        metric_value=60.0,
        metric_unit="dB",
        status="ok",
        computed_at="2026-06-18T10:01:00+08:00",
    )
    with pytest.raises(Exception):
        HistoricalMetricCacheTable.create(
            audio_data_id="a1",
            file_path="x.wav",
            file_size=10,
            file_mtime_ns=20,
            record_date="2026-06-18T10:00:00+08:00",
            product_model="M1",
            config_hash="cfg",
            calibration_hash="cal",
            algorithm_version="2026-06-18.1",
            channel_index=0,
            channel_label="CH1",
            metric_key="spl_peak_db",
            metric_label="声压级峰值",
            metric_value=60.0,
            metric_unit="dB",
            status="ok",
            computed_at="2026-06-18T10:01:00+08:00",
        )
```

Add `import pytest` to the test file.

- [ ] **Step 2: Run the new schema test and verify it fails**

Run: `pytest tests/test_historical_metric_cache_table.py -v`

Expected: FAIL because `model.historical_metric_cache_table` does not exist.

- [ ] **Step 3: Implement the Peewee cache model**

Create `model/historical_metric_cache_table.py`:

```python
from peewee import AutoField, BigIntegerField, FloatField, IntegerField, TextField

from model.base_mode import BaseModel


class HistoricalMetricCacheTable(BaseModel):
    cache_id = AutoField()
    audio_data_id = TextField(index=True)
    file_path = TextField()
    file_size = BigIntegerField()
    file_mtime_ns = BigIntegerField()
    record_date = TextField()
    product_model = TextField()
    barcode = TextField(null=True)
    label = TextField(null=True)
    label_source = TextField(null=True)
    analyse_config = TextField(null=True)
    config_hash = TextField()
    calibration_hash = TextField()
    algorithm_version = TextField()
    channel_index = IntegerField()
    channel_label = TextField()
    metric_key = TextField()
    metric_label = TextField()
    metric_value = FloatField(null=True)
    metric_unit = TextField()
    status = TextField()
    error_message = TextField(null=True)
    computed_at = TextField()

    class Meta:
        table_name = "historical_metric_cache_table"
        indexes = (
            (
                (
                    "audio_data_id",
                    "config_hash",
                    "algorithm_version",
                    "channel_index",
                    "metric_key",
                ),
                True,
            ),
            (("product_model", "record_date"), False),
            (("metric_key", "status"), False),
        )
```

- [ ] **Step 4: Bind the model in tests**

Modify `tests/conftest.py`:

```python
from model.historical_metric_cache_table import HistoricalMetricCacheTable
```

Add `HistoricalMetricCacheTable` to `ALL_TABLES` immediately after `AudioDataTable`.

- [ ] **Step 5: Update startup schema tests before implementation**

Modify `tests/test_db_connection_service.py`:

```python
EXPECTED_RUNTIME_TABLES.add("historical_metric_cache_table")

HISTORICAL_METRIC_CACHE_COLUMN_SQL = {
    "cache_id": "INTEGER NOT NULL PRIMARY KEY",
    "audio_data_id": "TEXT NOT NULL",
    "file_path": "TEXT NOT NULL",
    "file_size": "INTEGER NOT NULL",
    "file_mtime_ns": "INTEGER NOT NULL",
    "record_date": "TEXT NOT NULL",
    "product_model": "TEXT NOT NULL",
    "barcode": "TEXT",
    "label": "TEXT",
    "label_source": "TEXT",
    "analyse_config": "TEXT",
    "config_hash": "TEXT NOT NULL",
    "calibration_hash": "TEXT NOT NULL",
    "algorithm_version": "TEXT NOT NULL",
    "channel_index": "INTEGER NOT NULL",
    "channel_label": "TEXT NOT NULL",
    "metric_key": "TEXT NOT NULL",
    "metric_label": "TEXT NOT NULL",
    "metric_value": "REAL",
    "metric_unit": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    "error_message": "TEXT",
    "computed_at": "TEXT NOT NULL",
}
```

Extend `_create_table()` so the new table is created with the column SQL. Extract `_create_historical_metric_cache_indexes(tmp_db)` and call it from `_create_table()` for the new table; the helper creates these indexes:

```python
CREATE UNIQUE INDEX historical_metric_cache_unique_metric
ON historical_metric_cache_table (
    audio_data_id, config_hash, algorithm_version, channel_index, metric_key
)
CREATE INDEX historical_metric_cache_audio_data_id
ON historical_metric_cache_table (audio_data_id)
CREATE INDEX historical_metric_cache_product_date
ON historical_metric_cache_table (product_model, record_date)
CREATE INDEX historical_metric_cache_metric_status
ON historical_metric_cache_table (metric_key, status)
```

Add a test:

```python
def test_connect_existing_prepared_database_accepts_historical_metric_cache_table(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    svc_mod.DbConnectionService().connect()

    columns = _table_columns(tmp_db, "historical_metric_cache_table")
    assert columns["audio_data_id"][2] == "TEXT"
    assert columns["audio_data_id"][3] == 1
    assert columns["file_mtime_ns"][2] == "INTEGER"
    assert columns["file_mtime_ns"][3] == 1
    assert columns["metric_value"][2] == "REAL"
    assert columns["metric_value"][3] == 0
    assert columns["cache_id"][5] == 1
    indexes = _index_definitions(tmp_db, "historical_metric_cache_table")
    assert (
        "historical_metric_cache_unique_metric",
        (
            "audio_data_id",
            "config_hash",
            "algorithm_version",
            "channel_index",
            "metric_key",
        ),
        True,
    ) in indexes
    assert any(
        columns == ("metric_key", "status") and unique is False
        for _name, columns, unique in indexes
    )
    tmp_db.close()
```

Add negative startup-schema tests:

```python
def test_connect_rejects_missing_historical_metric_cache_column_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"historical_metric_cache_table"}):
        _create_table(tmp_db, table_name)
    broken_columns = {
        name: definition
        for name, definition in HISTORICAL_METRIC_CACHE_COLUMN_SQL.items()
        if name != "calibration_hash"
    }
    tmp_db.execute_sql(
        "CREATE TABLE historical_metric_cache_table ("
        + ", ".join(f"{name} {definition}" for name, definition in broken_columns.items())
        + ")"
    )
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="missing required historical_metric_cache_table"):
        svc_mod.DbConnectionService().connect()


def test_connect_rejects_missing_historical_metric_cache_unique_index_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.execute_sql("DROP INDEX historical_metric_cache_unique_metric")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="missing required historical_metric_cache_table indexes"):
        svc_mod.DbConnectionService().connect()


def test_connect_rejects_wrong_historical_metric_cache_column_definition_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"historical_metric_cache_table"}):
        _create_table(tmp_db, table_name)
    broken_columns = dict(HISTORICAL_METRIC_CACHE_COLUMN_SQL)
    broken_columns["metric_value"] = "TEXT"
    tmp_db.execute_sql(
        "CREATE TABLE historical_metric_cache_table ("
        + ", ".join(f"{name} {definition}" for name, definition in broken_columns.items())
        + ")"
    )
    _create_historical_metric_cache_indexes(tmp_db)
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="invalid historical_metric_cache_table column definitions"):
        svc_mod.DbConnectionService().connect()
```

- [ ] **Step 6: Run startup tests and verify failure**

Run: `pytest tests/test_db_connection_service.py::test_connect_existing_prepared_database_accepts_historical_metric_cache_table -v`

Expected: FAIL until `DbConnectionService` requires the new table and verifies the cache table schema.

- [ ] **Step 7: Implement startup table, column, and index requirements**

Modify `app/service/db_connection_service.py`:

```python
# Add "historical_metric_cache_table" to the existing REQUIRED_RUNTIME_TABLES set.

_REQUIRED_HISTORICAL_METRIC_CACHE_COLUMNS = frozenset(
    {
        "cache_id",
        "audio_data_id",
        "file_path",
        "file_size",
        "file_mtime_ns",
        "record_date",
        "product_model",
        "barcode",
        "label",
        "label_source",
        "analyse_config",
        "config_hash",
        "calibration_hash",
        "algorithm_version",
        "channel_index",
        "channel_label",
        "metric_key",
        "metric_label",
        "metric_value",
        "metric_unit",
        "status",
        "error_message",
        "computed_at",
    }
)
_REQUIRED_HISTORICAL_METRIC_CACHE_INDEXES = {
    (
        (
            "audio_data_id",
            "config_hash",
            "algorithm_version",
            "channel_index",
            "metric_key",
        ),
        True,
    ),
    (("audio_data_id",), False),
    (("product_model", "record_date"), False),
    (("metric_key", "status"), False),
}
_REQUIRED_HISTORICAL_METRIC_CACHE_COLUMN_DEFINITIONS = {
    "cache_id": ("INTEGER", True, True),
    "audio_data_id": ("TEXT", True, False),
    "file_path": ("TEXT", True, False),
    "file_size": ("INTEGER", True, False),
    "file_mtime_ns": ("INTEGER", True, False),
    "record_date": ("TEXT", True, False),
    "product_model": ("TEXT", True, False),
    "barcode": ("TEXT", False, False),
    "label": ("TEXT", False, False),
    "label_source": ("TEXT", False, False),
    "analyse_config": ("TEXT", False, False),
    "config_hash": ("TEXT", True, False),
    "calibration_hash": ("TEXT", True, False),
    "algorithm_version": ("TEXT", True, False),
    "channel_index": ("INTEGER", True, False),
    "channel_label": ("TEXT", True, False),
    "metric_key": ("TEXT", True, False),
    "metric_label": ("TEXT", True, False),
    "metric_value": ("REAL", False, False),
    "metric_unit": ("TEXT", True, False),
    "status": ("TEXT", True, False),
    "error_message": ("TEXT", False, False),
    "computed_at": ("TEXT", True, False),
}
```

Extend `_ensure_required_runtime_columns()`:

```python
self._ensure_required_table_columns(
    "historical_metric_cache_table",
    self._REQUIRED_HISTORICAL_METRIC_CACHE_COLUMNS,
)
self._ensure_historical_metric_cache_column_definitions()
self._ensure_historical_metric_cache_indexes()
```

Add `_ensure_historical_metric_cache_indexes()` and `_table_index_definitions(table_name)` helpers using `PRAGMA index_list` and `PRAGMA index_info`.
The index check must validate the unique metric key and the three query-support indexes. Do not create or migrate the table or indexes here.
Add `_ensure_historical_metric_cache_column_definitions()` using `PRAGMA table_info`; validate each required column's SQLite type, `NOT NULL` flag, and primary-key flag against `_REQUIRED_HISTORICAL_METRIC_CACHE_COLUMN_DEFINITIONS`.

- [ ] **Step 8: Add optional explicit DB preparation utility if needed**

If the implementer needs a reproducible way to prepare a local existing DB, create `tools/prepare_historical_metric_cache_table.py`. The script must:

- accept `--db D:\path\jingchen.db`
- open that file with `sqlite3`
- run `CREATE TABLE IF NOT EXISTS historical_metric_cache_table` with all columns from `HISTORICAL_METRIC_CACHE_COLUMN_SQL`
- run `CREATE UNIQUE INDEX IF NOT EXISTS historical_metric_cache_unique_metric` and the three non-unique indexes from Task 1
- print the changed DB path
- never run from `DbConnectionService.connect()`

- [ ] **Step 9: Run focused tests**

Run:

```bash
pytest tests/test_historical_metric_cache_table.py tests/test_db_connection_service.py -v
```

Expected: PASS, or only unrelated pre-existing failures explicitly documented.

- [ ] **Step 10: Commit**

```bash
git add model/historical_metric_cache_table.py tests/conftest.py tests/test_historical_metric_cache_table.py tests/test_db_connection_service.py app/service/db_connection_service.py
git add tools/prepare_historical_metric_cache_table.py
git commit -m "feat: add historical metric cache table"
```

Skip the optional `tools/...` path in `git add` if the utility was not created.

---

### Task 2: Report Service Data Model, Filters, And Cache Signatures

**Files:**
- Create: `app/service/historical_metric_report_types.py`
- Create: `app/service/historical_metric_report_cache.py`
- Create: `app/service/historical_metric_report_service.py`
- Create: `tests/test_historical_metric_report_service.py`
- Modify: `app/service/audio_data_service.py` only if a public query helper is needed

- [ ] **Step 1: Write failing tests for filter reuse and hashes**

In `tests/test_historical_metric_report_service.py`, create helpers:

```python
import json
from types import SimpleNamespace

import pytest

from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_types import (
    HISTORICAL_REPORT_ALGORITHM_VERSION,
)
from app.service.historical_metric_report_service import (
    HistoricalMetricReportService,
)
from model.audio_data_table import AudioDataTable


def _create_audio_row(**overrides):
    audio_data_id = overrides.pop("audio_data_id", "audio-1")
    payload = {
        "audio_data_id": audio_data_id,
        "file_path": overrides.pop("file_path", f"missing-{audio_data_id}.wav"),
        "product_model": overrides.pop("product_model", "M1"),
        "sample_rate": overrides.pop("sample_rate", 48000),
        "record_date": overrides.pop("record_date", "2026-06-18T10:00:00+08:00"),
        "labels": overrides.pop("labels", "OK"),
        "label_source": overrides.pop("label_source", "auto"),
        "barcode": overrides.pop("barcode", "SN-1"),
        "analyse_config": overrides.pop("analyse_config", "Config A"),
        "config_content": overrides.pop("config_content", "{}"),
    }
    payload.update(overrides)
    return AudioDataTable.create(**payload)


def _service(mic_cal=None, judgment_engine=None, algorithm_version=HISTORICAL_REPORT_ALGORITHM_VERSION):
    hss = SimpleNamespace(
        load_io_devices=lambda: {"mic_cal": {} if mic_cal is None else mic_cal}
    )
    return HistoricalMetricReportService(
        audio_data_service=AudioDataService(),
        hss_service=hss,
        judgment_engine=judgment_engine,
        algorithm_version=algorithm_version,
    )
```

Add tests:

```python
def test_report_service_reuses_audio_data_filter_semantics():
    _create_audio_row(
        audio_data_id="ok",
        product_model="M1",
        labels="OK",
        barcode="ABC",
        record_date="2026-06-18T10:00:00+08:00",
    )
    _create_audio_row(
        audio_data_id="ng",
        product_model="M2",
        labels="NG",
        barcode="DEF",
        record_date="2026-06-18T10:01:00+08:00",
    )
    _create_audio_row(
        audio_data_id="blank",
        product_model="M1",
        labels="",
        barcode="ABC-X",
        record_date="2026-06-18T10:02:00+08:00",
    )

    rows = _service().list_source_records(
        {
            "product_model": "M1",
            "start_time": "",
            "end_time": "",
            "barcode": "ABC",
            "labels": ["OK", "not_labeled"],
        }
    )

    assert [row.audio_data_id for row in rows] == ["blank", "ok"]


def test_report_service_applies_time_range_filter_semantics():
    _create_audio_row(
        audio_data_id="early",
        record_date="2026-06-18T09:59:59+08:00",
    )
    _create_audio_row(
        audio_data_id="inside",
        record_date="2026-06-18T10:30:00+08:00",
    )
    _create_audio_row(
        audio_data_id="late",
        record_date="2026-06-18T11:00:01+08:00",
    )

    rows = _service().list_source_records(
        {
            "product_model": "ALL",
            "start_time": "2026-06-18 10:00:00",
            "end_time": "2026-06-18 11:00:00",
            "barcode": "",
            "labels": ["OK", "NG", "not_labeled"],
        }
    )

    assert [row.audio_data_id for row in rows] == ["inside"]


def test_config_hash_is_deterministic_for_json_content():
    service = _service()

    left = service.config_hash(json.dumps({"b": 2, "a": 1}, ensure_ascii=False))
    right = service.config_hash(json.dumps({"a": 1, "b": 2}, ensure_ascii=False))

    assert left == right


def test_calibration_hash_changes_with_mic_calibration():
    assert _service({"In1": {"sensitivity": 1.0}}).current_calibration_hash() != (
        _service({"In1": {"sensitivity": 2.0}}).current_calibration_hash()
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_historical_metric_report_service.py::test_report_service_reuses_audio_data_filter_semantics tests/test_historical_metric_report_service.py::test_report_service_applies_time_range_filter_semantics tests/test_historical_metric_report_service.py::test_config_hash_is_deterministic_for_json_content tests/test_historical_metric_report_service.py::test_calibration_hash_changes_with_mic_calibration -v
```

Expected: FAIL because the service module does not exist.

- [ ] **Step 3: Implement shared types and service/cache skeletons**

Create `app/service/historical_metric_report_types.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


HISTORICAL_REPORT_ALGORITHM_VERSION = "2026-06-18.1"
RECORD_STATUS_METRIC_KEY = "__record__"
METRIC_DEFINITIONS = {
    "spl_peak_db": ("声压级峰值", "dB"),
    "spl_average_db": ("平均声压级", "dB"),
    "loudness_peak_sone": ("响度峰值", "sone"),
    "loudness_average_sone": ("平均响度", "sone"),
    "stft_score": ("频谱最低得分", ""),
    "vibration_rms": ("振动 RMS", ""),
    "vibration_peak": ("振动峰值", ""),
    "vibration_peak_factor": ("振动峰值因子", ""),
    "vibration_main_frequency_hz": ("振动主频", "Hz"),
}


class ReportCanceled(Exception):
    pass


@dataclass(frozen=True)
class SourceSignature:
    file_path: str
    file_size: int
    file_mtime_ns: int
    config_hash: str
    calibration_hash: str
    algorithm_version: str


@dataclass(frozen=True)
class HistoricalMetricValue:
    audio_data_id: str
    record_date: str
    product_model: str
    barcode: str | None
    label: str | None
    label_source: str | None
    analyse_config: str | None
    channel_index: int
    channel_label: str
    metric_key: str
    metric_label: str
    metric_value: float | None
    metric_unit: str
    status: str
    error_message: str | None = None


@dataclass(frozen=True)
class MetricGroupStats:
    group_key: str
    product_model: str
    metric_key: str
    metric_label: str
    metric_unit: str
    channel_label: str
    n: int
    mean: float | None
    std: float | None
    min: float | None
    max: float | None
    p5: float | None
    p50: float | None
    p95: float | None
    histogram_bins: list[float] = field(default_factory=list)
    histogram_counts: list[int] = field(default_factory=list)
    normal_x: list[float] = field(default_factory=list)
    normal_y: list[float] = field(default_factory=list)
    normal_available: bool = False


@dataclass(frozen=True)
class HistoricalMetricReportResult:
    filters: dict[str, object]
    generated_at: str
    total_records: int
    valid_record_count: int
    skipped_record_count: int
    failed_record_count: int
    label_counts: dict[str, int]
    metric_rows: list[HistoricalMetricValue]
    stats: list[MetricGroupStats]
    failures: list[HistoricalMetricValue]
    outliers: list[HistoricalMetricValue]


ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]
```

Create `app/service/historical_metric_report_cache.py` with:

```python
from __future__ import annotations

import hashlib
import json

from app.service.historical_metric_report_types import SourceSignature


class HistoricalMetricCacheComputer:
    def __init__(self, *, hss_service, judgment_engine=None, algorithm_version: str):
        self._hss_service = hss_service
        self._judgment_engine = judgment_engine
        self._algorithm_version = str(algorithm_version)

    def current_mic_calibration(self) -> dict:
        try:
            devices = self._hss_service.load_io_devices()
        except Exception:
            return {}
        mic_cal = devices.get("mic_cal") if isinstance(devices, dict) else None
        return mic_cal if isinstance(mic_cal, dict) else {}

    def current_calibration_hash(self) -> str:
        return _stable_hash(self.current_mic_calibration())

    def config_hash(self, config_content: object) -> str:
        text = "" if config_content is None else str(config_content)
        if not text.strip():
            return "__missing_config__"
        try:
            parsed = json.loads(text)
        except Exception:
            return "__invalid_config__:" + _sha256_text(text)
        return _stable_hash(parsed)


def _stable_hash(value: object) -> str:
    return _sha256_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

Create `app/service/historical_metric_report_service.py` with:

```python
from __future__ import annotations

from app.service.analysis.judgment import JudgmentEngine
from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_cache import HistoricalMetricCacheComputer
from app.service.historical_metric_report_types import (
    HISTORICAL_REPORT_ALGORITHM_VERSION,
)


class HistoricalMetricReportService:
    def __init__(
        self,
        *,
        audio_data_service: AudioDataService,
        hss_service,
        judgment_engine=None,
        ai_service=None,
        golden_sample_service=None,
        algorithm_version: str = HISTORICAL_REPORT_ALGORITHM_VERSION,
    ):
        if judgment_engine is None:
            judgment_engine = JudgmentEngine(
                ai_service,
                golden_sample_service=golden_sample_service,
            )
        self._audio_data_service = audio_data_service
        self._cache = HistoricalMetricCacheComputer(
            hss_service=hss_service,
            judgment_engine=judgment_engine,
            algorithm_version=algorithm_version,
        )

    def list_source_records(self, filters: dict[str, object] | None):
        return list(self._audio_data_service._filtered_query(filters))

    def config_hash(self, config_content: object) -> str:
        return self._cache.config_hash(config_content)

    def current_calibration_hash(self) -> str:
        return self._cache.current_calibration_hash()
```

- [ ] **Step 4: Verify responsibilities stay split**

At the end of this task:

- `historical_metric_report_types.py` contains dataclasses/constants/exceptions only
- `historical_metric_report_cache.py` contains hash/calibration helpers and will receive cache/backfill logic in Task 3
- `historical_metric_report_service.py` contains filter querying and delegates hash helpers to `HistoricalMetricCacheComputer`
- no WAV analysis, statistics, HTML export, or PyQt code is in `historical_metric_report_service.py`

- [ ] **Step 5: Run focused tests**

Run the tests from Step 2 again.

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/service/historical_metric_report_types.py app/service/historical_metric_report_cache.py app/service/historical_metric_report_service.py tests/test_historical_metric_report_service.py
git commit -m "feat: add historical report service foundation"
```

---

### Task 3: Cache Validity, Lazy Backfill, And Metric Extraction

**Files:**
- Modify: `app/service/historical_metric_report_cache.py`
- Modify: `app/service/historical_metric_report_service.py`
- Modify: `app/service/historical_metric_report_types.py` only if result/type fields need adjustment
- Modify: `tests/test_historical_metric_report_service.py`

- [ ] **Step 1: Add failing tests for cache validity and failure rows**

Add tests covering:

```python
def test_missing_wav_writes_failed_record_cache_row():
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.1,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(config_content=json.dumps(config, ensure_ascii=False))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert result.failures[0].channel_index == -1
    assert result.failures[0].metric_key == "__record__"
    assert "WAV" in result.failures[0].error_message or "文件" in result.failures[0].error_message


def test_invalid_config_writes_failed_record_cache_row():
    _create_audio_row(config_content="{bad json")

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert result.failures[0].status == "failed"
    assert "配置" in result.failures[0].error_message
```

- [ ] **Step 2: Add failing tests for projected channel mapping**

Use `scipy.io.wavfile.write()` to create a one-column WAV and a config with `selected_channels=[3]`.

Monkeypatch expensive analysis functions if necessary:

```python
def test_projected_runtime_wav_maps_selected_channel_to_column_zero(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [3],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 3}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    seen = {}

    class FakeJudgment:
        def rejudge(self, *args, channel_column_map=None, **kwargs):
            seen["channel_column_map"] = channel_column_map
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=3,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                        spl_average_db=60.5,
                        loudness_max_sone=2.0,
                        loudness_average_sone=1.5,
                    )
                ],
            )

    service = HistoricalMetricReportService(
        audio_data_service=AudioDataService(),
        hss_service=SimpleNamespace(load_io_devices=lambda: {"mic_cal": {}}),
        judgment_engine=FakeJudgment(),
    )
    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {3: {"stft_score": {"summary_score": 88.0}}},
    )

    result = service.generate_report({"labels": ["OK", "NG"]})

    assert seen["channel_column_map"] == {3: 0}
    assert {row.metric_key for row in result.metric_rows if row.status == "ok"} >= {
        "spl_peak_db",
        "spl_average_db",
        "loudness_peak_sone",
        "loudness_average_sone",
        "stft_score",
    }
```

Add the opposite test:

```python
def test_wav_channel_count_must_match_selected_channels(tmp_path):
    wav_path = tmp_path / "two_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 2), dtype=np.float32))
    config = {
        "selected_channels": [3],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 3}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert "通道" in result.failures[0].error_message
```

Add vibration extraction tests:

```python
def test_vibration_metrics_are_extracted_for_valid_vibration_channel(tmp_path, monkeypatch):
    wav_path = tmp_path / "two_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 2), dtype=np.float32))
    config = {
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}, {"channel_index": 1}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {
            1: {
                "vibration": {
                    "overall_rms": 1.25,
                    "overall_peak": 2.5,
                    "overall_crest_factor": 2.0,
                    "dominant_frequency_hz": 60.0,
                }
            }
        },
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report({"labels": ["OK", "NG"]})

    by_key = {row.metric_key: row for row in result.metric_rows if row.status == "ok"}
    assert by_key["vibration_rms"].metric_label == "振动 RMS"
    assert by_key["vibration_rms"].metric_value == pytest.approx(1.25)
    assert by_key["vibration_peak"].metric_label == "振动峰值"
    assert by_key["vibration_peak"].metric_value == pytest.approx(2.5)
    assert by_key["vibration_peak_factor"].metric_label == "振动峰值因子"
    assert by_key["vibration_peak_factor"].metric_value == pytest.approx(2.0)
    assert by_key["vibration_main_frequency_hz"].metric_label == "振动主频"
    assert by_key["vibration_main_frequency_hz"].metric_unit == "Hz"
    assert by_key["vibration_main_frequency_hz"].metric_value == pytest.approx(60.0)


def test_vibration_metrics_are_omitted_without_valid_vibration_channel(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "vibration_view_channel_index": 3,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {0: {"vibration": {"overall_rms": 1.25}}},
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report({"labels": ["OK", "NG"]})

    assert all(
        not row.metric_key.startswith("vibration_")
        for row in result.metric_rows
    )
```

Add an acoustic exclusion test:

```python
def test_acoustic_metrics_exclude_configured_vibration_channel(tmp_path, monkeypatch):
    wav_path = tmp_path / "two_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 2), dtype=np.float32))
    config = {
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}, {"channel_index": 1}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=0,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                        spl_average_db=60.5,
                        loudness_max_sone=2.0,
                        loudness_average_sone=1.5,
                    ),
                    ChannelJudgment(
                        channel_index=1,
                        spl_db=90.0,
                        spl_pass=True,
                        spl_max_db=91.0,
                        spl_average_db=90.5,
                        loudness_max_sone=9.0,
                        loudness_average_sone=8.5,
                    ),
                ],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {
            0: {"stft_score": {"summary_score": 88.0}},
            1: {"stft_score": {"summary_score": 1.0}},
        },
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report({"labels": ["OK", "NG"]})

    acoustic_keys = {
        "spl_peak_db",
        "spl_average_db",
        "loudness_peak_sone",
        "loudness_average_sone",
        "stft_score",
    }
    assert all(
        row.channel_index != 1
        for row in result.metric_rows
        if row.metric_key in acoustic_keys
    )
```

- [ ] **Step 3: Add failing cache validity and invalidation tests**

```python
def test_cache_recomputes_when_calibration_hash_changes(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    _service({"In1": {"sensitivity": 1.0}}, judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )
    _service({"In1": {"sensitivity": 2.0}}, judgment_engine=FakeJudgment()).generate_report(
        {"labels": ["OK", "NG"]}
    )

    assert len(calls) == 2
```

Add file metadata, config hash, algorithm version, and stale-row cleanup tests:

```python
def test_cache_reuses_valid_rows_without_reanalysis(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    service = _service(judgment_engine=FakeJudgment())

    service.generate_report({"labels": ["OK", "NG"]})
    service.generate_report({"labels": ["OK", "NG"]})

    assert len(calls) == 1


def test_cache_recomputes_when_file_identity_changes(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    service = _service(judgment_engine=FakeJudgment())

    service.generate_report({"labels": ["OK", "NG"]})
    before = wav_path.stat()
    wavfile.write(wav_path, 1000, np.zeros((2000, 1), dtype=np.float32))
    after = wav_path.stat()
    assert (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)
    service.generate_report({"labels": ["OK", "NG"]})

    assert len(calls) == 2


def test_cache_recomputes_when_config_hash_changes(tmp_path, monkeypatch):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    row = _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )
    service = _service(judgment_engine=FakeJudgment())

    service.generate_report({"labels": ["OK", "NG"]})
    config["analysis_duration"] = 0.25
    AudioDataTable.update(config_content=json.dumps(config)).where(
        AudioDataTable.audio_data_id == row.audio_data_id
    ).execute()
    service.generate_report({"labels": ["OK", "NG"]})

    assert len(calls) == 2


def test_cache_recomputes_and_deletes_stale_rows_when_algorithm_version_changes(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))
    calls = []

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            calls.append(args)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        lambda **kwargs: {},
    )

    _service(judgment_engine=FakeJudgment(), algorithm_version="test.v1").generate_report(
        {"labels": ["OK", "NG"]}
    )
    _service(judgment_engine=FakeJudgment(), algorithm_version="test.v2").generate_report(
        {"labels": ["OK", "NG"]}
    )

    assert len(calls) == 2
    assert {
        row.algorithm_version for row in HistoricalMetricCacheTable.select()
    } == {"test.v2"}


def test_generate_report_raises_report_canceled_when_cancel_requested():
    _create_audio_row(audio_data_id="a1")

    with pytest.raises(ReportCanceled):
        _service().generate_report(
            {"labels": ["OK", "NG"]},
            cancel_check=lambda: True,
        )
```

Add a read-failure sentinel test:

```python
def test_invalid_wav_read_failure_writes_sentinel_file_identity(tmp_path):
    wav_path = tmp_path / "bad.wav"
    wav_path.write_bytes(b"not a wav")
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    row = HistoricalMetricCacheTable.get()
    assert row.status == "failed"
    assert row.file_path.endswith("bad.wav")
    assert row.file_size == -1
    assert row.file_mtime_ns == -1
```

Add metric algorithm failure tests:

```python
def test_rejudge_failure_writes_failed_row_and_continues_to_next_record(tmp_path):
    first_wav = tmp_path / "first.wav"
    second_wav = tmp_path / "second.wav"
    wavfile.write(first_wav, 1000, np.zeros((1000, 1), dtype=np.float32))
    wavfile.write(second_wav, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(audio_data_id="bad", file_path=str(first_wav), config_content=json.dumps(config))
    _create_audio_row(audio_data_id="good", file_path=str(second_wav), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, config, buffer, sample_rate, mic_cal, audio_file_path, *args, **kwargs):
            if audio_file_path == str(first_wav):
                raise RuntimeError("judgment failed")
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[ChannelJudgment(channel_index=0, spl_db=60.0, spl_pass=True)],
            )

    result = _service(judgment_engine=FakeJudgment()).generate_report({"labels": ["OK", "NG"]})

    assert result.failed_record_count == 1
    assert result.valid_record_count == 1
    assert any("judgment failed" in (row.error_message or "") for row in result.failures)


def test_chart_payload_failure_preserves_judgment_metrics_and_records_failure(
    tmp_path,
    monkeypatch,
):
    wav_path = tmp_path / "one_channel.wav"
    wavfile.write(wav_path, 1000, np.zeros((1000, 1), dtype=np.float32))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": 0.5,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    class FakeJudgment:
        def rejudge(self, *args, **kwargs):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.5),
                channel_results=[
                    ChannelJudgment(
                        channel_index=0,
                        spl_db=60.0,
                        spl_pass=True,
                        spl_max_db=61.0,
                        spl_average_db=60.5,
                    )
                ],
            )

    def fail_chart(**kwargs):
        raise RuntimeError("chart failed")

    monkeypatch.setattr(
        "app.service.historical_metric_report_cache.build_chart_payload",
        fail_chart,
    )

    result = _service(judgment_engine=FakeJudgment()).generate_report({"labels": ["OK", "NG"]})

    ok_keys = {row.metric_key for row in result.metric_rows if row.status == "ok"}
    assert {"spl_peak_db", "spl_average_db"} <= ok_keys
    assert any("chart failed" in (row.error_message or "") for row in result.failures)
```

Add a real lazy-backfill test that does not monkeypatch analysis:

```python
def test_lazy_backfill_real_small_wav_generates_cache_rows(tmp_path):
    sample_rate = 2000
    duration_sec = 0.5
    times = np.arange(int(sample_rate * duration_sec), dtype=np.float64) / sample_rate
    signal = (0.01 * np.sin(2 * np.pi * 100.0 * times)).astype(np.float32)
    wav_path = tmp_path / "small.wav"
    wavfile.write(wav_path, sample_rate, signal.reshape(-1, 1))
    config = {
        "selected_channels": [0],
        "analysis_window_offset_ms": 0,
        "analysis_duration": duration_sec,
        "channels": [{"channel_index": 0}],
    }
    _create_audio_row(file_path=str(wav_path), config_content=json.dumps(config))

    result = _service().generate_report({"labels": ["OK", "NG"]})

    ok_keys = {row.metric_key for row in result.metric_rows if row.status == "ok"}
    assert {"spl_peak_db", "spl_average_db"} <= ok_keys
    assert result.valid_record_count == 1
    assert HistoricalMetricCacheTable.select().where(
        HistoricalMetricCacheTable.status == "ok"
    ).count() >= 2
```

Adjust helper `_service()` to accept `judgment_engine` and `algorithm_version`.
Add `import numpy as np`, `from scipy.io import wavfile`, and `from model.historical_metric_cache_table import HistoricalMetricCacheTable` to the test file.
Also import `METRIC_DEFINITIONS` and `ReportCanceled` from `app.service.historical_metric_report_types`.
Add `from app.service.analysis.frames import ChannelJudgment, JudgmentResult` for tests that use fake judgment results.

- [ ] **Step 4: Run new tests and verify failure**

Run:

```bash
pytest tests/test_historical_metric_report_service.py -k "missing_wav or invalid_config or projected_runtime or channel_count or vibration_metrics or acoustic_metrics or calibration_hash or file_identity or invalid_wav or rejudge_failure or chart_payload_failure or config_hash or algorithm_version or reuses_valid or report_canceled or real_small_wav or summary_counts" -v
```

Expected: FAIL until backfill and cache logic exists.

- [ ] **Step 5: Implement source signatures**

In `HistoricalMetricCacheComputer`, add:

```python
def source_signature_for_record(self, record: AudioDataTable) -> SourceSignature:
    normalized_path = _normalize_file_path(record.file_path)
    file_size, file_mtime_ns = _file_identity(normalized_path)
    return SourceSignature(
        file_path=normalized_path,
        file_size=file_size,
        file_mtime_ns=file_mtime_ns,
        config_hash=self.config_hash(record.config_content),
        calibration_hash=self.current_calibration_hash(),
        algorithm_version=self._algorithm_version,
    )
```

Expose a pass-through on `HistoricalMetricReportService` only for tests and orchestration:

```python
def source_signature_for_record(self, record: AudioDataTable) -> SourceSignature:
    return self._cache.source_signature_for_record(record)
```

Rules:

- missing/empty path: `file_path=""`, `file_size=-1`, `file_mtime_ns=-1`
- path present but stat/read fails: normalized path, `file_size=-1`, `file_mtime_ns=-1`
- missing config: `config_hash="__missing_config__"`
- invalid config: `config_hash="__invalid_config__:<sha256 raw text>"`
- missing calibration: hash of `{}`

Use a two-stage source identity:

- the normal source signature uses `Path.stat()` for existing files
- if existing cache rows for the record contain `file_size=-1` or `file_mtime_ns=-1`, call `load_wav()` in a cheap validation branch:
  - if `load_wav()` still fails, compare against a sentinel signature using the normalized path and `file_size=-1`, `file_mtime_ns=-1`
  - if `load_wav()` succeeds, treat the cache as stale and recompute with the real stat identity
  - this makes persistent unreadable files stable in the report while allowing a repaired WAV file to invalidate the old failed row

- [ ] **Step 6: Implement cache validity and record-scoped refresh**

Add methods:

```python
def valid_cache_rows_for_record(self, record, signature):
    rows = list(
        HistoricalMetricCacheTable.select()
        .where(HistoricalMetricCacheTable.audio_data_id == record.audio_data_id)
        .order_by(HistoricalMetricCacheTable.channel_index, HistoricalMetricCacheTable.metric_key)
    )
    if not rows:
        return []
    for row in rows:
        if not self._row_matches_signature(row, signature):
            return []
    return rows


def refresh_record_cache(self, record, signature) -> list[HistoricalMetricCacheTable]:
    rows = self._compute_record_cache_rows(record, signature)
    with db.atomic():
        HistoricalMetricCacheTable.delete().where(
            HistoricalMetricCacheTable.audio_data_id == record.audio_data_id
        ).execute()
        for payload in rows:
            HistoricalMetricCacheTable.create(**payload)
    return list(
        HistoricalMetricCacheTable.select()
        .where(HistoricalMetricCacheTable.audio_data_id == record.audio_data_id)
        .order_by(HistoricalMetricCacheTable.channel_index, HistoricalMetricCacheTable.metric_key)
    )
```

These methods belong in `HistoricalMetricCacheComputer`; `HistoricalMetricReportService.generate_report()` calls them.

- [ ] **Step 7: Implement strict historical computation**

In `HistoricalMetricCacheComputer._compute_record_cache_rows()`:

1. Parse config JSON; if missing/invalid, return record-level failure.
2. Validate `selected_channels = selected_config_channels(config)` is non-empty.
3. Load WAV with `load_wav(record.file_path)`. If it raises, return a record-level failure row whose source identity uses the normalized path with `file_size=-1` and `file_mtime_ns=-1`.
4. Require `wav.channel_count == len(selected_channels)`.
5. Build `channel_column_map = {selected: column for column, selected in enumerate(selected_channels)}`.
6. Resolve strict window:

```python
start_sec = float(config.get("analysis_window_offset_ms", 0) or 0) / 1000.0
duration_sec = float(config.get("analysis_duration", 0) or 0)
end_sec = start_sec + duration_sec
if start_sec < 0 or duration_sec <= 0 or end_sec > wav.duration_sec:
    return [record_failure("分析窗口超出 WAV 时长或无效")]
```

7. Run:

```python
loudness_trace_cache = {}
judgment_result = self._judgment_engine.rejudge(
    config,
    wav.buffer,
    wav.sample_rate,
    mic_cal,
    wav.path,
    start_sec,
    end_sec,
    run_ai=False,
    allow_golden_runtime_resample=True,
    channel_column_map=channel_column_map,
    loudness_trace_cache=loudness_trace_cache,
)
judgment_payload = asdict(judgment_result)
chart_payload = build_chart_payload(
    buffer=wav.buffer,
    config=config,
    sample_rate=wav.sample_rate,
    mic_cal=mic_cal,
    analysis_window=(start_sec, end_sec),
    judgment_payload=judgment_payload,
    allow_golden_runtime_resample=True,
    channel_column_map=channel_column_map,
    loudness_trace_cache=loudness_trace_cache,
)
```

Wrap per-record metric algorithms:

- If `JudgmentEngine.rejudge()` raises, catch the exception and return a single record-level `failed` sentinel row for that audio record; continue processing remaining matched records.
- If `build_chart_payload()` raises, keep any SPL/loudness values already available from `judgment_result`, add a record-level `failed` sentinel row with the chart error message, and continue processing remaining matched records.
- Never let one record's metric algorithm exception abort the full report.

8. Extract finite metric rows:

Use `METRIC_DEFINITIONS[metric_key]` for every row's `metric_label` and `metric_unit`; do not duplicate Chinese labels in multiple helper functions.

- Define `acoustic_channels = selected_channels` excluding a valid configured `vibration_view_channel_index`.
- `spl_peak_db`: `spl_max_db`, fallback finite `spl_db`, for acoustic channels only
- `spl_average_db`: `spl_average_db`, for acoustic channels only
- `loudness_peak_sone`: `loudness_max_sone`, for acoustic channels only
- `loudness_average_sone`: `loudness_average_sone`, for acoustic channels only
- `stft_score`: `chart_payload[channel]["stft_score"]["summary_score"]`, for acoustic channels only
- vibration metrics only for valid `vibration_view_channel_index`:
  - `vibration_rms`: `chart_payload[vibration_channel]["vibration"]["overall_rms"]`
  - `vibration_peak`: `chart_payload[vibration_channel]["vibration"]["overall_peak"]`
  - `vibration_peak_factor`: `chart_payload[vibration_channel]["vibration"]["overall_crest_factor"]`
  - `vibration_main_frequency_hz`: `chart_payload[vibration_channel]["vibration"]["dominant_frequency_hz"]`

Only write rows for finite numeric values. If `dominant_frequency_hz` is `None`, omit `vibration_main_frequency_hz` for that record/channel rather than writing a failed metric row.

- [ ] **Step 8: Implement row conversion and report generation**

`generate_report(filters, progress_callback=None, cancel_check=None)` should:

- call `list_source_records(filters)`
- if `cancel_check()` returns true before record processing begins, raise `ReportCanceled`
- for each record, use valid cache rows or recompute
- call `progress_callback(done, total, audio_data_id)` after each record if provided
- if `cancel_check()` returns true before starting the next record or immediately after progress is emitted, raise `ReportCanceled`
- convert cache rows to `HistoricalMetricValue`, using current `audio_data_table` fields for display/filter metadata
- aggregate statistics in Task 4 helpers
- compute summary counters by distinct `audio_data_id`, not cache-row count:
  - `total_records`: number of matched `audio_data_table` rows
  - `valid_record_count`: number of matched records with at least one `ok` metric row
  - `skipped_record_count`: number of matched records with at least one `skipped` cache row
  - `failed_record_count`: number of matched records with at least one `failed` cache row
  - `label_counts`: counts over matched source records using current labels, with empty/null/`not_labeled` counted as `not_labeled`

Add a summary-counter test:

```python
def test_generate_report_summary_counts_are_distinct_records_not_metric_rows():
    ok_record = _create_audio_row(audio_data_id="ok", labels="OK")
    ng_record = _create_audio_row(audio_data_id="ng", labels="NG")
    blank_record = _create_audio_row(audio_data_id="blank", labels="")
    service = _service()
    for record, status, metric_key in (
        (ok_record, "ok", "spl_peak_db"),
        (ok_record, "ok", "spl_average_db"),
        (ng_record, "failed", "__record__"),
        (blank_record, "skipped", "__record__"),
    ):
        current_signature = service.source_signature_for_record(record)
        HistoricalMetricCacheTable.create(
            audio_data_id=record.audio_data_id,
            file_path=current_signature.file_path,
            file_size=current_signature.file_size,
            file_mtime_ns=current_signature.file_mtime_ns,
            record_date=record.record_date,
            product_model=record.product_model,
            barcode=record.barcode,
            label=record.labels,
            label_source=record.label_source,
            analyse_config=record.analyse_config,
            config_hash=current_signature.config_hash,
            calibration_hash=current_signature.calibration_hash,
            algorithm_version=current_signature.algorithm_version,
            channel_index=-1 if metric_key == "__record__" else 0,
            channel_label="记录" if metric_key == "__record__" else "CH1",
            metric_key=metric_key,
            metric_label="记录状态" if metric_key == "__record__" else METRIC_DEFINITIONS[metric_key][0],
            metric_value=None if status != "ok" else 60.0,
            metric_unit="" if metric_key == "__record__" else METRIC_DEFINITIONS[metric_key][1],
            status=status,
            error_message="测试原因" if status != "ok" else None,
            computed_at="2026-06-18T10:05:00+08:00",
        )

    result = service.generate_report({"labels": ["OK", "NG", "not_labeled"]})

    assert result.total_records == 3
    assert result.valid_record_count == 1
    assert result.failed_record_count == 1
    assert result.skipped_record_count == 1
    assert result.label_counts == {"OK": 1, "NG": 1, "not_labeled": 1}
```

- [ ] **Step 9: Run focused tests**

Run:

```bash
pytest tests/test_historical_metric_report_service.py -k "missing_wav or invalid_config or projected_runtime or channel_count or vibration_metrics or acoustic_metrics or calibration_hash or file_identity or invalid_wav or rejudge_failure or chart_payload_failure or config_hash or algorithm_version or reuses_valid or report_canceled or real_small_wav or summary_counts" -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/service/historical_metric_report_cache.py app/service/historical_metric_report_service.py app/service/historical_metric_report_types.py tests/test_historical_metric_report_service.py
git commit -m "feat: backfill historical metric cache"
```

---

### Task 4: Statistics, Histogram, Normal Curve, And Outliers

**Files:**
- Create: `app/service/historical_metric_report_statistics.py`
- Modify: `app/service/historical_metric_report_service.py`
- Modify: `tests/test_historical_metric_report_service.py`

- [ ] **Step 1: Write failing statistics tests**

Add tests:

```python
def test_statistics_use_sample_std_and_linear_percentiles():
    rows = [
        HistoricalMetricValue(
            audio_data_id=f"a{i}",
            record_date="2026-06-18T10:00:00+08:00",
            product_model="M1",
            barcode=None,
            label="OK",
            label_source="auto",
            analyse_config="Cfg",
            channel_index=0,
            channel_label="CH1",
            metric_key="spl_peak_db",
            metric_label="声压级峰值",
            metric_value=float(value),
            metric_unit="dB",
            status="ok",
        )
        for i, value in enumerate([1, 2, 3, 4, 5], start=1)
    ]

    stats = compute_statistics(rows)
    stat = next(item for item in stats if item.product_model == "M1")

    assert stat.n == 5
    assert stat.mean == pytest.approx(3.0)
    assert stat.std == pytest.approx(np.std([1, 2, 3, 4, 5], ddof=1))
    assert stat.p5 == pytest.approx(np.percentile([1, 2, 3, 4, 5], 5, method="linear"))
    assert stat.normal_available is True
```

Add these concrete tests:

```python
def _metric_row(audio_data_id, value, *, product_model="M1", metric_key="spl_peak_db"):
    return HistoricalMetricValue(
        audio_data_id=audio_data_id,
        record_date="2026-06-18T10:00:00+08:00",
        product_model=product_model,
        barcode=None,
        label="OK",
        label_source="auto",
        analyse_config="Cfg",
        channel_index=0,
        channel_label="CH1",
        metric_key=metric_key,
        metric_label="声压级峰值",
        metric_value=value,
        metric_unit="dB",
        status="ok",
    )


def test_statistics_ignore_non_finite_values():
    rows = [
        _metric_row("finite-1", 1.0),
        _metric_row("finite-2", 2.0),
        _metric_row("nan", float("nan")),
        _metric_row("inf", float("inf")),
    ]

    stat = next(item for item in compute_statistics(rows) if item.product_model == "M1")

    assert stat.n == 2
    assert stat.mean == pytest.approx(1.5)


def test_statistics_suppress_normal_curve_for_small_samples():
    stat = next(
        item
        for item in compute_statistics([_metric_row(f"a{i}", float(i)) for i in range(4)])
        if item.product_model == "M1"
    )

    assert stat.n == 4
    assert stat.normal_available is False
    assert stat.normal_x == []
    assert stat.normal_y == []


def test_statistics_include_empty_model_bucket_and_all_models_aggregate():
    rows = [
        _metric_row("blank", 1.0, product_model=""),
        _metric_row("m1", 3.0, product_model="M1"),
    ]

    stats = compute_statistics(rows)
    by_model = {item.product_model: item for item in stats}

    assert by_model["未填写型号"].n == 1
    assert by_model["未填写型号"].mean == pytest.approx(1.0)
    assert by_model["全部型号"].n == 2
    assert by_model["全部型号"].mean == pytest.approx(2.0)


def test_outliers_use_three_std_and_percentile_rules():
    three_std_rows = [
        _metric_row(f"std-{i}", 10.0, product_model="STD")
        for i in range(19)
    ] + [_metric_row("std-high", 100.0, product_model="STD")]
    percentile_rows = [
        _metric_row(f"pct-{i}", float(i), product_model="PCTL")
        for i in range(20)
    ]

    outliers = compute_outliers(three_std_rows + percentile_rows)
    outlier_ids = {row.audio_data_id for row in outliers}

    assert "std-high" in outlier_ids
    assert {"pct-0", "pct-19"} <= outlier_ids
```

Import `compute_statistics` and `compute_outliers` from `app.service.historical_metric_report_statistics` in this test file.

- [ ] **Step 2: Run statistics tests and verify failure**

Run:

```bash
pytest tests/test_historical_metric_report_service.py -k "statistics or outlier or insufficient or finite" -v
```

Expected: FAIL until stats helpers exist.

- [ ] **Step 3: Implement statistics helpers**

Create `app/service/historical_metric_report_statistics.py` and add:

```python
def compute_statistics(rows: list[HistoricalMetricValue]) -> list[MetricGroupStats]:
    ok_rows = [
        row for row in rows
        if row.status == "ok" and _finite(row.metric_value)
    ]
    groups = _group_metric_rows(ok_rows, include_all_models=True)
    return [_compute_group_stats(group_key, group_rows) for group_key, group_rows in groups]
```

Use:

```python
std = float(np.std(values, ddof=1)) if n >= 2 else None
p5, p50, p95 = np.percentile(values, [5, 50, 95], method="linear")
```

If current NumPy raises `TypeError` for `method=`, fall back to `interpolation="linear"`.

For normal curve:

- require `n >= 5`
- require finite positive `std`
- build histogram with `np.histogram(values, bins="auto")`
- build normal x over min/max or `mean +/- 4std`
- scale normal density to histogram counts using `bin_width * n`

- [ ] **Step 4: Implement outlier detection**

Add a module function with this exact signature: `compute_outliers(rows: list[HistoricalMetricValue]) -> list[HistoricalMetricValue]`.

Rules:

- evaluate per product model / metric key / channel label group
- flag outside `mean +/- 3 * std` when std is available
- also flag outside P5/P95 when `n >= 20`
- de-duplicate by `(audio_data_id, channel_index, metric_key)`
- do not change labels or cache rows

- [ ] **Step 5: Wire stats/outliers into `generate_report()`**

The returned `HistoricalMetricReportResult` must contain:

- `metric_rows`: all cache rows used by the report, including ok/skipped/failed
- `stats`: ok finite numeric rows only
- `failures`: skipped/failed rows
- `outliers`: ok rows flagged by report annotations

`HistoricalMetricReportService.generate_report()` imports and calls `compute_statistics()` and `compute_outliers()`; do not move the math helpers into the service module.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_historical_metric_report_service.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/service/historical_metric_report_statistics.py app/service/historical_metric_report_service.py tests/test_historical_metric_report_service.py
git commit -m "feat: aggregate historical metric distributions"
```

---

### Task 5: HTML And CSV Export

**Files:**
- Create: `app/service/historical_metric_report_export.py`
- Create: `tests/test_historical_metric_report_export.py`

- [ ] **Step 1: Write failing export tests**

Create tests:

```python
from app.service.historical_metric_report_export import (
    export_csv_detail,
    export_html_report,
)
from app.service.historical_metric_report_types import (
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    MetricGroupStats,
)


def _metric_row(
    *,
    audio_data_id="a1",
    status="ok",
    metric_value=60.0,
    error_message=None,
):
    return HistoricalMetricValue(
        audio_data_id=audio_data_id,
        record_date="2026-06-18T10:00:00+08:00",
        product_model="M1",
        barcode="SN-1",
        label="OK",
        label_source="auto",
        analyse_config="Cfg",
        channel_index=0,
        channel_label="CH1",
        metric_key="spl_peak_db",
        metric_label="声压级峰值",
        metric_value=metric_value,
        metric_unit="dB",
        status=status,
        error_message=error_message,
    )


def _stats_row():
    return MetricGroupStats(
        group_key="M1|spl_peak_db|CH1",
        product_model="M1",
        metric_key="spl_peak_db",
        metric_label="声压级峰值",
        metric_unit="dB",
        channel_label="CH1",
        n=5,
        mean=60.0,
        std=1.5,
        min=58.0,
        max=62.0,
        p5=58.4,
        p50=60.0,
        p95=61.6,
        histogram_bins=[58.0, 59.0, 60.0, 61.0, 62.0],
        histogram_counts=[1, 1, 1, 1],
        normal_x=[58.0, 60.0, 62.0],
        normal_y=[0.1, 0.3, 0.1],
        normal_available=True,
    )


def _sample_report_result(metric_rows=None, stats=None, failures=None, outliers=None):
    rows = list(metric_rows or [_metric_row()])
    failure_rows = list(
        failures
        if failures is not None
        else [row for row in rows if row.status in {"skipped", "failed"}]
    )
    return HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=2,
        valid_record_count=1,
        skipped_record_count=sum(1 for row in rows if row.status == "skipped"),
        failed_record_count=sum(1 for row in rows if row.status == "failed"),
        label_counts={"OK": 1, "NG": 0, "not_labeled": 0},
        metric_rows=rows,
        stats=list(stats or [_stats_row()]),
        failures=failure_rows,
        outliers=list(outliers or []),
    )


def test_csv_export_uses_utf8_bom_and_includes_failure_rows(tmp_path):
    result = _sample_report_result(
        metric_rows=[
            _metric_row(status="ok", metric_value=60.0),
            _metric_row(status="failed", metric_value=None, error_message="WAV 文件不存在"),
        ]
    )
    target = tmp_path / "report.csv"

    export_csv_detail(result, str(target))

    content = target.read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")
    text = content.decode("utf-8-sig")
    assert "audio_data_id" in text
    assert "WAV 文件不存在" in text


def test_html_export_is_static_and_contains_summary_and_chart_data(tmp_path):
    failed = _metric_row(
        audio_data_id="a2",
        status="failed",
        metric_value=None,
        error_message="WAV 文件不存在",
    )
    result = _sample_report_result(metric_rows=[_metric_row(), failed])
    target = tmp_path / "report.html"

    export_html_report(result, str(target))

    text = target.read_text(encoding="utf-8")
    assert "测量结果正态分布统计报告" in text
    assert "声压级峰值" in text
    assert "失败" in text
    assert "http://" not in text
    assert "https://" not in text
```

- [ ] **Step 2: Run export tests and verify failure**

Run: `pytest tests/test_historical_metric_report_export.py -v`

Expected: FAIL because export module does not exist.

- [ ] **Step 3: Implement CSV export**

In `app/service/historical_metric_report_export.py`:

```python
CSV_HEADERS = [
    "audio_data_id",
    "record_date",
    "product_model",
    "barcode",
    "label",
    "label_source",
    "analyse_config",
    "channel_label",
    "metric_key",
    "metric_label",
    "metric_value",
    "metric_unit",
    "status",
    "error_message",
]


def export_csv_detail(result: HistoricalMetricReportResult, target_path: str) -> str:
    with open(target_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in result.metric_rows:
            writer.writerow(_metric_row_to_csv_dict(row))
    return target_path
```

Use empty string for `None` and preserve failed/skipped rows.

- [ ] **Step 4: Implement static HTML export**

Implement:

```python
def export_html_report(result: HistoricalMetricReportResult, target_path: str) -> str:
    html = render_html_report(result)
    Path(target_path).write_text(html, encoding="utf-8")
    return target_path
```

HTML requirements:

- title `测量结果正态分布统计报告`
- generated time
- filter summary
- coverage summary
- per-metric statistics table
- inline SVG histogram/normal-fit chart for each `MetricGroupStats`
- failure table
- outlier table
- no external CSS/JS/images

Use `html.escape()` for all user/file/model/barcode/error text.

- [ ] **Step 5: Run export tests**

Run: `pytest tests/test_historical_metric_report_export.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/service/historical_metric_report_export.py tests/test_historical_metric_report_export.py
git commit -m "feat: export historical distribution report"
```

---

### Task 6: Native Report Dialog And Background Worker

**Files:**
- Create: `ui/popups/historical_metric_report_dialog.py`
- Create: `tests/test_historical_metric_report_dialog.py`

- [ ] **Step 1: Write failing dialog tests**

Create UI tests with `QT_QPA_PLATFORM=offscreen`:

```python
from types import SimpleNamespace

from PyQt6.QtCore import QDateTime

from app.service.historical_metric_report_types import (
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    MetricGroupStats,
    ReportCanceled,
)
from ui.popups.historical_metric_report_dialog import (
    HistoricalMetricReportDialog,
    HistoricalReportWorkerThread,
)


class _SignalStub:
    def __init__(self):
        self._callbacks = []
    def connect(self, callback):
        self._callbacks.append(callback)
    def emit(self, *args):
        for callback in list(self._callbacks):
            callback(*args)


class _AudioDataServiceStub:
    def __init__(self, product_models=None):
        self._product_models = [] if product_models is None else list(product_models)

    def list_product_models(self):
        return list(self._product_models)


class _ReportServiceStub:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def generate_report(self, filters, progress_callback=None, cancel_check=None):
        self.calls.append(dict(filters))
        if progress_callback is not None:
            progress_callback(1, 1, "stub-audio")
        if self.result is not None:
            return self.result
        return HistoricalMetricReportResult(
            filters=dict(filters),
            generated_at="2026-06-18T10:05:00+08:00",
            total_records=0,
            valid_record_count=0,
            skipped_record_count=0,
            failed_record_count=0,
            label_counts={"OK": 0, "NG": 0, "not_labeled": 0},
            metric_rows=[],
            stats=[],
            failures=[],
            outliers=[],
        )


def _dialog(*, product_models=None, report_service=None):
    return HistoricalMetricReportDialog(
        services=SimpleNamespace(
            audio_data_service=_AudioDataServiceStub(product_models=product_models),
            historical_metric_report_service=report_service or _ReportServiceStub(),
        )
    )


def test_report_dialog_defaults_to_all_models_no_time_ok_ng(qapp):
    service = _ReportServiceStub()
    dialog = HistoricalMetricReportDialog(
        services=SimpleNamespace(
            audio_data_service=_AudioDataServiceStub(),
            historical_metric_report_service=service,
        )
    )

    assert dialog.combo_product_model.currentText() == "ALL"
    assert dialog.cb_enable_time_range.isChecked() is False
    assert dialog.cb_ok.isChecked() is True
    assert dialog.cb_ng.isChecked() is True
    assert dialog.cb_not_labeled.isChecked() is False


def test_report_dialog_populates_product_models_from_audio_data_service(qapp):
    dialog = HistoricalMetricReportDialog(
        services=SimpleNamespace(
            audio_data_service=_AudioDataServiceStub(product_models=["M1", "M2"]),
            historical_metric_report_service=_ReportServiceStub(),
        )
    )

    assert [
        dialog.combo_product_model.itemText(index)
        for index in range(dialog.combo_product_model.count())
    ] == ["ALL", "M1", "M2"]


def test_report_dialog_collects_filters_without_selected_rows(qapp):
    dialog = _dialog()

    filters = dialog.current_filters()

    assert filters["product_model"] == "ALL"
    assert filters["start_time"] == ""
    assert filters["end_time"] == ""
    assert filters["labels"] == ["OK", "NG"]


def test_report_dialog_serializes_enabled_time_range(qapp):
    dialog = _dialog()
    dialog.cb_enable_time_range.setChecked(True)
    dialog.dt_start_time.setDateTime(
        QDateTime.fromString("2026-06-18 10:00:00", "yyyy-MM-dd HH:mm:ss")
    )
    dialog.dt_end_time.setDateTime(
        QDateTime.fromString("2026-06-18 11:00:00", "yyyy-MM-dd HH:mm:ss")
    )

    filters = dialog.current_filters()

    assert filters["start_time"] == "2026-06-18 10:00:00"
    assert filters["end_time"] == "2026-06-18 11:00:00"
```

Add concrete render/export tests:

```python
def _report_result_with_stats_and_failure():
    failure = HistoricalMetricValue(
        audio_data_id="failed-1",
        record_date="2026-06-18T10:00:00+08:00",
        product_model="M1",
        barcode="SN-F",
        label="NG",
        label_source="manual",
        analyse_config="Cfg",
        channel_index=-1,
        channel_label="记录",
        metric_key="__record__",
        metric_label="记录状态",
        metric_value=None,
        metric_unit="",
        status="failed",
        error_message="WAV 文件不存在",
    )
    return HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=3,
        valid_record_count=2,
        skipped_record_count=0,
        failed_record_count=1,
        label_counts={"OK": 2, "NG": 1, "not_labeled": 0},
        metric_rows=[failure],
        stats=[
            MetricGroupStats(
                group_key="全部型号|CH1|spl_peak_db",
                product_model="全部型号",
                metric_key="spl_peak_db",
                metric_label="声压级峰值",
                metric_unit="dB",
                channel_label="CH1",
                n=5,
                mean=60.0,
                std=1.5,
                min=58.0,
                max=62.0,
                p5=58.2,
                p50=60.0,
                p95=61.8,
                histogram_bins=[58.0, 60.0, 62.0],
                histogram_counts=[2, 3],
                normal_x=[58.0, 60.0, 62.0],
                normal_y=[1.0, 3.0, 1.0],
                normal_available=True,
            )
        ],
        failures=[failure],
        outliers=[],
    )


def test_report_dialog_render_result_fills_summary_and_stats_table(qapp):
    dialog = _dialog()

    dialog._render_result(_report_result_with_stats_and_failure())

    assert dialog.lb_total_records.text() == "3"
    assert dialog.lb_valid_records.text() == "2"
    assert dialog.lb_failed_records.text() == "1"
    assert dialog.lb_label_counts.text() == "OK 2 / NG 1 / 未标注 0"
    assert dialog.stats_table.rowCount() == 1
    assert dialog.stats_table.item(0, 0).text() == "全部型号"
    assert dialog.stats_table.item(0, 1).text() == "CH1"
    assert dialog.stats_table.item(0, 2).text() == "声压级峰值"
    assert dialog.stats_table.item(0, 3).text() == "5"
    assert dialog.stats_table.item(0, 4).text() == "60.000"


def test_report_dialog_failures_table_shows_reasons(qapp):
    dialog = _dialog()

    dialog._render_result(_report_result_with_stats_and_failure())

    assert dialog.failures_table.rowCount() == 1
    assert dialog.failures_table.item(0, 0).text() == "failed-1"
    assert dialog.failures_table.item(0, 5).text() == "failed"
    assert dialog.failures_table.item(0, 6).text() == "WAV 文件不存在"


def test_report_dialog_export_calls_html_and_csv_exporters(qapp, monkeypatch, tmp_path):
    calls = []
    html_path = tmp_path / "historical-report.html"
    dialog = _dialog()
    dialog._render_result(_report_result_with_stats_and_failure())

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(html_path), "HTML"),
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.export_html_report",
        lambda result, path: calls.append(("html", result, path)) or path,
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.export_csv_detail",
        lambda result, path: calls.append(("csv", result, path)) or path,
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.QMessageBox.information",
        lambda *args, **kwargs: None,
    )

    dialog.btn_export.click()

    assert [(kind, path) for kind, _result, path in calls] == [
        ("html", str(html_path)),
        ("csv", str(tmp_path / "historical-report.csv")),
    ]
    assert all(result is dialog._current_result for _kind, result, _path in calls)
```

Add concrete worker/generate/cancel tests below. These cover:

- clicking generate starts background computation and progress updates the dialog
- cancel requests interruption on the worker and leaves controls enabled after the worker emits `canceled`
- the worker catches `ReportCanceled` and emits `canceled`
- empty reports with no valid metrics still show failures and keep export available

For the worker cancellation test, use a service stub that raises `ReportCanceled`:

```python
def test_report_worker_emits_canceled_for_report_canceled(qapp):
    class CancelingService:
        def generate_report(self, *args, **kwargs):
            raise ReportCanceled()

    worker = HistoricalReportWorkerThread(CancelingService(), {"labels": ["OK", "NG"]})
    emissions = []
    worker.canceled.connect(lambda: emissions.append("canceled"))

    worker.run()

    assert emissions == ["canceled"]
```

The imports and `_dialog()`, `_ReportServiceStub`, `_AudioDataServiceStub`, and `_SignalStub` helpers are defined at the top of the test file above.

Add a generate/progress UI test:

```python
def test_report_dialog_generate_starts_worker_and_updates_progress(qapp, monkeypatch):
    starts = []

    class FakeWorker:
        def __init__(self, report_service, filters):
            self.report_service = report_service
            self.filters = filters
            self.progress_changed = _SignalStub()
            self.succeeded = _SignalStub()
            self.failed = _SignalStub()
            self.canceled = _SignalStub()
        def start(self):
            starts.append(self.filters)
            self.progress_changed.emit(1, 3, "audio-1")
        def isRunning(self):
            return True
        def requestInterruption(self):
            pass
        def deleteLater(self):
            pass

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.HistoricalReportWorkerThread",
        FakeWorker,
    )
    dialog = _dialog()

    dialog.btn_generate.click()

    assert starts and starts[0]["labels"] == ["OK", "NG"]
    assert "1 / 3" in dialog.lb_progress.text()
    assert "audio-1" in dialog.lb_progress.text()
```

Add a dialog cancel test:

```python
def test_report_dialog_cancel_requests_worker_and_restores_controls(qapp, monkeypatch):
    interrupted = []
    workers = []

    class FakeWorker:
        def __init__(self, report_service, filters):
            self.progress_changed = _SignalStub()
            self.succeeded = _SignalStub()
            self.failed = _SignalStub()
            self.canceled = _SignalStub()
            workers.append(self)
        def start(self):
            pass
        def isRunning(self):
            return True
        def requestInterruption(self):
            interrupted.append(True)
        def deleteLater(self):
            pass

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.HistoricalReportWorkerThread",
        FakeWorker,
    )
    dialog = _dialog()

    dialog.btn_generate.click()
    dialog.btn_cancel.click()
    workers[0].canceled.emit()

    assert interrupted == [True]
    assert dialog.btn_generate.isEnabled() is True
    assert dialog.btn_cancel.isEnabled() is False
```

Add an empty-report state test:

```python
def test_report_dialog_empty_result_shows_failures_and_allows_export(qapp, monkeypatch, tmp_path):
    failure = HistoricalMetricValue(
        audio_data_id="a1",
        record_date="2026-06-18T10:00:00+08:00",
        product_model="M1",
        barcode="SN-1",
        label="OK",
        label_source="auto",
        analyse_config="Cfg",
        channel_index=-1,
        channel_label="记录",
        metric_key="__record__",
        metric_label="记录状态",
        metric_value=None,
        metric_unit="",
        status="failed",
        error_message="WAV 文件不存在",
    )
    result = HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=1,
        valid_record_count=0,
        skipped_record_count=0,
        failed_record_count=1,
        label_counts={"OK": 1, "NG": 0, "not_labeled": 0},
        metric_rows=[failure],
        stats=[],
        failures=[failure],
        outliers=[],
    )
    dialog = _dialog()

    dialog._render_result(result)

    assert dialog.stats_table.rowCount() == 0
    assert dialog.failures_table.rowCount() == 1
    assert dialog.btn_export.isEnabled() is True
```

- [ ] **Step 2: Run dialog tests and verify failure**

Run: `pytest tests/test_historical_metric_report_dialog.py -v`

Expected: FAIL because dialog module does not exist.

- [ ] **Step 3: Implement worker thread**

In `ui/popups/historical_metric_report_dialog.py`:

```python
class HistoricalReportWorkerThread(QThread):
    progress_changed = pyqtSignal(int, int, str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    canceled = pyqtSignal()

    def __init__(self, report_service, filters):
        super().__init__()
        self._report_service = report_service
        self._filters = dict(filters)

    def run(self):
        try:
            result = self._report_service.generate_report(
                self._filters,
                progress_callback=self.progress_changed.emit,
                cancel_check=self.isInterruptionRequested,
            )
        except ReportCanceled:
            self.canceled.emit()
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)
```

`ReportCanceled` is defined in `app/service/historical_metric_report_types.py`; import it in this dialog module and in the dialog tests.

- [ ] **Step 4: Implement dialog layout**

Use a compact native layout:

- top filter toolbar:
  - `combo_product_model`
  - `cb_enable_time_range`
  - `dt_start_time`
  - `dt_end_time`
  - `ed_barcode`
  - `cb_ok`, `cb_ng`, `cb_not_labeled`
  - `btn_generate`
  - `btn_cancel`
  - `btn_export`
- summary row labels:
  - total records
  - valid records
  - skipped records
  - failed records
  - OK/NG/unlabeled counts
- main `QSplitter` or `QTabWidget`:
  - statistics `QTableWidget`
  - `pyqtgraph.PlotWidget` histogram/normal curve
  - failures table
  - outliers table

Set object names on key widgets for tests:

```python
self.lb_total_records.setObjectName("HistoricalReportTotalRecords")
self.lb_valid_records.setObjectName("HistoricalReportValidRecords")
self.lb_failed_records.setObjectName("HistoricalReportFailedRecords")
self.lb_label_counts.setObjectName("HistoricalReportLabelCounts")
self.btn_generate.setObjectName("GenerateHistoricalReportButton")
self.btn_export.setObjectName("ExportHistoricalReportButton")
self.lb_progress.setObjectName("HistoricalReportProgressLabel")
self.stats_table.setObjectName("HistoricalReportStatsTable")
self.failures_table.setObjectName("HistoricalReportFailuresTable")
```

Use these table column orders so tests and exports stay stable:

- `stats_table`: `型号`, `通道`, `指标`, `样本数`, `均值`, `标准差`, `P5`, `P50`, `P95`
- `failures_table`: `记录ID`, `时间`, `型号`, `条码`, `指标`, `状态`, `原因`
- `outliers_table`: `记录ID`, `时间`, `型号`, `通道`, `指标`, `数值`, `单位`

Populate the model combo during initialization:

```python
self.combo_product_model.addItem("ALL")
for model in self.services.audio_data_service.list_product_models():
    self.combo_product_model.addItem(str(model))
```

If `list_product_models()` raises, keep only `ALL` and show no blocking error; report generation can still run unfiltered.

Implement `current_filters()` with the same date string format used by `AudioDataFilterDialog`:

```python
return {
    "product_model": self.combo_product_model.currentText(),
    "start_time": (
        self.dt_start_time.dateTime().toString("yyyy-MM-dd HH:mm:ss")
        if self.cb_enable_time_range.isChecked()
        else ""
    ),
    "end_time": (
        self.dt_end_time.dateTime().toString("yyyy-MM-dd HH:mm:ss")
        if self.cb_enable_time_range.isChecked()
        else ""
    ),
    "barcode": self.ed_barcode.text().strip(),
    "labels": labels,
}
```

Generate/progress behavior:

- `_generate_report()` creates `HistoricalReportWorkerThread(self.services.historical_metric_report_service, self.current_filters())`
- disable `btn_generate`, enable `btn_cancel`, and reset `lb_progress` before `start()`
- connect `progress_changed(done, total, audio_data_id)` to a slot that writes text like `1 / 3  audio-1`
- on `succeeded`, store `_current_result`, call `_render_result(result)`, enable export, and restore controls
- on `failed`/`canceled`, restore controls without destroying the dialog

- [ ] **Step 5: Implement plot rendering**

When a statistics row is selected:

- clear the `PlotWidget`
- draw histogram using `pg.BarGraphItem`
- draw normal curve using `plot()` when `normal_available`
- show a small native label with "样本不足，未显示正态曲线" when normal curve is unavailable
- if `result.stats` is empty, leave the chart empty and show an empty-state label while still rendering `result.failures`
- keep axes labels compact and avoid layout jumps

- [ ] **Step 6: Implement export action**

On export:

- require a completed report result
- prompt for an HTML file path using `QFileDialog.getSaveFileName`
- derive CSV sibling path from the HTML stem, e.g. `report.csv`
- call:

```python
export_html_report(self._current_result, html_path)
export_csv_detail(self._current_result, csv_path)
```

- show success/failure via `QMessageBox`

- [ ] **Step 7: Run dialog tests**

Run: `pytest tests/test_historical_metric_report_dialog.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add ui/popups/historical_metric_report_dialog.py tests/test_historical_metric_report_dialog.py app/service/historical_metric_report_service.py
git commit -m "feat: add native historical report dialog"
```

---

### Task 7: Data Management Entry Point And Service Registration

**Files:**
- Modify: `app/services_container.py`
- Modify: `ui/modules/data_management.py`
- Modify: `tests/test_data_management_module.py`
- Modify: `tests/test_service_container.py` if service construction expectations need updating

- [ ] **Step 1: Write failing service-container and data-management tests**

Add this to `tests/test_service_container.py`:

```python
def test_service_container_constructs_historical_report_service_without_circular_import(
    monkeypatch,
):
    from app.service.historical_metric_report_service import HistoricalMetricReportService
    import app.service.factory_mode_controller as factory_mode_controller

    class FakeDb:
        def connect(self):
            pass
        def close(self):
            pass

    class FakeConfigService:
        golden_sample_service = object()

    class FakeAiService:
        def shutdown(self):
            pass

    monkeypatch.setattr(mod, "DbConnectionService", FakeDb)
    monkeypatch.setattr(mod, "HardwareSelectService", lambda: object())
    monkeypatch.setattr(mod, "InputCalibrationService", lambda bus: object())
    monkeypatch.setattr(mod, "ConfigService", lambda bus: FakeConfigService())
    monkeypatch.setattr(mod, "AudioDataService", lambda: object())
    monkeypatch.setattr(mod, "AiService", FakeAiService)
    monkeypatch.setattr(
        mod,
        "ConfigPackageService",
        lambda *, config_service, ai_service: object(),
    )
    monkeypatch.setattr(mod, "FactorySelectionSettings", lambda: object())
    monkeypatch.setattr(mod, "TestManager", lambda bus, services: object())
    monkeypatch.setattr(
        factory_mode_controller,
        "FactoryModeController",
        lambda bus, services: object(),
    )

    services = mod.ServiceContainer()

    assert isinstance(
        services.historical_metric_report_service,
        HistoricalMetricReportService,
    )
    assert (
        services.historical_metric_report_service._audio_data_service
        is services.audio_data_service
    )
```

Update `_AudioDataServiceStub` in `tests/test_data_management_module.py` so it supports current pagination:

```python
def count_records(self, _filters=None):
    return len(self.list_records(_filters))


def list_records_page(self, filters=None, page=1, page_size=50):
    start = (page - 1) * page_size
    end = start + page_size
    return self.list_records(filters)[start:end]
```

Add these to `tests/test_data_management_module.py`:

```python
def test_data_management_shows_statistics_report_button(qapp):
    services = SimpleNamespace(
        audio_data_service=_AudioDataServiceStub(),
        historical_metric_report_service=object(),
    )
    module = DataManagementModule(services)

    assert module.btn_statistics_report.text() == "统计报告"


def test_statistics_report_opens_without_selected_rows(qapp, monkeypatch):
    opened = []

    class FakeDialog:
        def __init__(self, services, parent=None):
            self.destroyed = SimpleNamespace(connect=lambda callback: None)
            opened.append((services, parent))
        def show(self):
            opened.append("show")
        def raise_(self):
            opened.append("raise")
        def activateWindow(self):
            opened.append("activate")

    monkeypatch.setattr(
        "ui.modules.data_management.HistoricalMetricReportDialog",
        FakeDialog,
    )
    services = SimpleNamespace(
        audio_data_service=_AudioDataServiceStub(),
        historical_metric_report_service=object(),
    )
    module = DataManagementModule(services)

    module.btn_statistics_report.click()

    assert opened[0][0] is services
    assert "show" in opened
```

- [ ] **Step 2: Run service-container and data-management tests and verify failure**

Run: `pytest tests/test_service_container.py::test_services_container_cold_imports_in_subprocess tests/test_service_container.py::test_service_container_constructs_historical_report_service_without_circular_import tests/test_data_management_module.py -v`

Expected: FAIL until the lazy service registration, button, and dialog import exist.

- [ ] **Step 3: Register service**

Modify `app/services_container.py`:

Do not add a top-level import for `HistoricalMetricReportService`.

After `self.ai_service = AiService()` and after `self.config_service` exists, add:

```python
from app.service.historical_metric_report_service import HistoricalMetricReportService

self.historical_metric_report_service = HistoricalMetricReportService(
    audio_data_service=self.audio_data_service,
    hss_service=self.hss_service,
    ai_service=self.ai_service,
    golden_sample_service=self.config_service.golden_sample_service,
)
```

Keep this lazy import and assignment after `hss_service`, `audio_data_service`, `config_service`, and `ai_service` are initialized. Do not register it before `ai_service`; the default runtime path must construct a real `JudgmentEngine` through `HistoricalMetricReportService`. The lazy import avoids the circular path `services_container -> historical_metric_report_service -> historical_metric_report_cache -> imported_audio -> services_container`.

- [ ] **Step 4: Add data-management toolbar action**

Modify `ui/modules/data_management.py` imports:

```python
from ui.popups.historical_metric_report_dialog import HistoricalMetricReportDialog
```

In `__init__`, add:

```python
self._report_dialog = None
```

In `_build_ui()`:

```python
self.btn_statistics_report = QPushButton("统计报告")
```

Add `self.btn_statistics_report` to `toolbar_layout` after `self.btn_filter` and before `self.btn_select_all`:

```python
toolbar_layout.addWidget(self.btn_statistics_report)
```

Connect it next to the existing toolbar signal connections:

```python
self.btn_statistics_report.clicked.connect(self._show_statistics_report)
```

Add:

```python
def _show_statistics_report(self) -> None:
    if self._report_dialog is None:
        self._report_dialog = HistoricalMetricReportDialog(self.services, parent=self)
        self._report_dialog.destroyed.connect(lambda *_: setattr(self, "_report_dialog", None))
    self._report_dialog.show()
    self._report_dialog.raise_()
    self._report_dialog.activateWindow()
```

Do not require selected rows.

- [ ] **Step 5: Run focused UI tests**

Run:

```bash
pytest tests/test_data_management_module.py tests/test_historical_metric_report_dialog.py -v
```

Expected: PASS.

- [ ] **Step 6: Run service-container tests if touched**

Run:

```bash
pytest tests/test_service_container.py -v
```

Expected: PASS, or documented unrelated pre-existing failures.

- [ ] **Step 7: Commit**

```bash
git add app/services_container.py ui/modules/data_management.py tests/test_data_management_module.py tests/test_service_container.py
git commit -m "feat: expose historical statistics report"
```

Skip `tests/test_service_container.py` in `git add` if it was not modified.

---

### Task 8: End-To-End Verification And Cleanup

**Files:**
- Modify only files required by failures discovered in this task.

- [ ] **Step 1: Run focused service/schema/export/UI tests**

Run:

```bash
pytest tests/test_historical_metric_cache_table.py tests/test_db_connection_service.py tests/test_historical_metric_report_service.py tests/test_historical_metric_report_export.py tests/test_historical_metric_report_dialog.py tests/test_data_management_module.py -v
```

Expected: PASS.

- [ ] **Step 2: Run nearby regression tests**

Run:

```bash
pytest tests/test_audio_data_service.py tests/test_imported_audio_analysis.py tests/test_judgment.py tests/test_review_payloads.py -v
```

Expected: PASS, or document any pre-existing unrelated failures with exact test names and messages.

- [ ] **Step 3: Manually smoke the UI if possible**

Start the app only if a prepared local database exists:

```bash
python main.py
```

Smoke path:

1. Open `数据管理`.
2. Click `统计报告`.
3. Generate with defaults.
4. Confirm the UI remains responsive while progress updates.
5. Confirm failures appear if historical WAVs/configs are missing.
6. Export HTML/CSV to a temporary path and open the HTML in a browser.

If no prepared DB is available, skip this with a clear note.

- [ ] **Step 4: Inspect changed files**

Run:

```bash
git status -sb
git diff --stat
git diff --check
```

Expected:

- only files in this plan changed
- no whitespace errors
- ignored/unrelated local files untouched

- [ ] **Step 5: Final commit**

If Task 8 required fixes, commit them:

```bash
git add model/historical_metric_cache_table.py app/service/db_connection_service.py app/service/historical_metric_report_types.py app/service/historical_metric_report_cache.py app/service/historical_metric_report_statistics.py app/service/historical_metric_report_service.py app/service/historical_metric_report_export.py app/services_container.py ui/popups/historical_metric_report_dialog.py ui/modules/data_management.py tests/conftest.py tests/test_historical_metric_cache_table.py tests/test_db_connection_service.py tests/test_historical_metric_report_service.py tests/test_historical_metric_report_export.py tests/test_historical_metric_report_dialog.py tests/test_data_management_module.py tests/test_service_container.py
git commit -m "test: verify historical distribution report"
```

If no fixes were required, do not create an empty commit.

---

## Handoff Notes For Implementers

- Use `subagent-driven-development` for execution.
- Execute tasks sequentially; after each task, run its focused tests and commit before continuing.
- Use the spec document as the source of truth when the plan and code disagree.
- If reviewer feedback asks to support full-hardware historical WAV channel layouts, reject that expansion for this implementation; the approved spec explicitly limits support to projected runtime WAVs.
- If startup tests reveal an existing prepared database file in `data/` lacks the cache table, do not commit binary DB changes unless the user explicitly asks. Use or add the optional preparation tool and report the manual preparation step.
