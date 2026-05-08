# Manual Judgment Source Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist manual OK/NG as the final judgment on the current test's single audio record, record whether the final judgment is automatic or manual, show that source in data management, and reset the page after successful manual judgment.

**Architecture:** Keep the final judgment on `audio_data_table` by adding nullable `label_source`. `TestSession` owns row-level persistence, `TestManager` owns session lookup and saved-row eligibility, `FactoryModeController` coordinates state/reset, and UI modules only trigger actions or render state.

**Tech Stack:** Python, PyQt6, Peewee ORM, SQLite, pytest.

---

Spec: `docs/double-sdd/specs/2026-05-08-manual-judgment-source-design.md`

## File Structure

- Modify `model/audio_data_table.py`
  - Add nullable `label_source = TextField(null=True)`.

- No change expected in `app/service/db_connection_service.py`
  - Fresh-schema creation is driven by the Peewee model.
  - Do not add migration/backfill for `audio_data_table.label_source`.

- Modify `app/service/audio_data_service.py`
  - Export/import-column reflection should naturally include `label_source`.
  - Update export model to clone `AudioDataTable.label_source`.

- Modify `ui/modules/data_management.py`
  - Add table header and min width for `label_source`.
  - Render source values as `自动` / `人工` / blank / raw unknown.

- Create `tests/test_audio_data_service.py`
  - Cover column list/export behavior including `label_source`.

- Create `tests/test_data_management_module.py`
  - Cover source header and display mapping.

- Modify `app/service/analysis/test_session.py`
  - Automatic result persistence writes `label_source="auto"` with `labels`.
  - Add a manual label update method on the saved row, returning a success/failure object or tuple.

- Modify `tests/test_test_session.py`
  - Add/adjust persistence tests for `label_source`.
  - Add manual label update success/failure tests.

- Modify `app/service/analysis/test_manager.py`
  - Add manual judgment API that resolves active/completed session by config/run and delegates to `TestSession`.
  - Reject no session, stale run, no saved audio id, missing row, invalid label, or update failure.
  - Retain stopped sessions in `_completed_sessions` when early-stop partial WAV save created an audio data row, so manual OK/NG can still update that row.

- Modify `tests/test_test_manager/test_review_payload_emission.py` or create `tests/test_test_manager/test_manual_judgment.py`
  - Prefer creating `tests/test_test_manager/test_manual_judgment.py` to keep this feature focused.

- Modify `app/service/factory_mode_controller.py`
  - Add controller-level manual judgment entry point.
  - Reject busy/no-display states.
  - On success call `clear_for_next_run("manual_judgment_ok" | "manual_judgment_ng")`.
  - Preserve stopped-run `run_id` in controller review context so manual judgment cannot label a stale stopped result.

- Modify `tests/test_factory_mode_controller/test_reanalysis_and_review.py` or create `tests/test_factory_mode_controller/test_manual_judgment.py`
  - Prefer creating `tests/test_factory_mode_controller/test_manual_judgment.py`.

- Modify `ui/modules/control.py`
  - Wire OK/NG buttons to the controller entry point.
  - Show visible warning on failure.

- Modify `tests/test_control_module.py`
  - Cover button calls and failure warning.

- Modify `ui/modules/workorder.py`
  - Subscribe to `factory_display_reset`.
  - Clear only SN on manual judgment reset reasons and publish/cache blank-SN payload.

- Modify `tests/test_workorder_module.py`
  - Cover manual reset clearing SN and non-manual reset preserving SN.

- Do not add manual-reset behavior to `ui/modules/video.py`.
  - The live camera preview is not per-unit result imagery.
  - Manual judgment reset must not clear, stop, start, refresh, or otherwise disturb `VideoModule`.

- Create `tests/test_video_module.py`.
  - Cover the inverse regression: with a fake `CameraThread` and local `ui.modules.video.cv2` patch, manual judgment display reset preserves a populated preview and the original camera thread without stop/restart/refresh.

## Task 1: Data Model And Data Management Source Display

**Files:**
- Modify: `model/audio_data_table.py`
- Modify: `app/service/audio_data_service.py`
- Modify: `ui/modules/data_management.py`
- Create: `tests/test_audio_data_service.py`
- Create: `tests/test_data_management_module.py`

- [ ] **Step 1: Write failing model/service tests**

Add `tests/test_audio_data_service.py` with tests like:

```python
from __future__ import annotations

import os
import sqlite3
import zipfile

from app.service.audio_data_service import AudioDataService
from model.audio_data_table import AudioDataTable


def test_audio_data_service_columns_include_label_source():
    columns = AudioDataService().list_column_names()

    assert "label_source" in columns
    assert columns.index("label_source") > columns.index("labels")


def test_audio_data_export_db_includes_label_source(tmp_path):
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFF")
    AudioDataTable.create(
        audio_data_id="audio-1",
        file_path=str(wav_path),
        product_model="M1",
        sample_rate=44100,
        record_date="2026-05-08T10:00:00+08:00",
        labels="OK",
        label_source="manual",
        barcode="SN-1",
        analyse_config="Config A",
        config_content="{}",
    )

    target = tmp_path / "export.zip"
    service = AudioDataService()
    records = service.list_records()
    service.export_zip(str(target), records)

    with zipfile.ZipFile(target) as archive:
        db_name = [name for name in archive.namelist() if name.endswith(".db")][0]
        archive.extract(db_name, tmp_path)
    con = sqlite3.connect(tmp_path / db_name)
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(audio_data_table)")]
        row = con.execute("SELECT label_source FROM audio_data_table").fetchone()
    finally:
        con.close()

    assert "label_source" in columns
    assert row == ("manual",)
```

Add `tests/test_data_management_module.py` with offscreen Qt setup and a fake service:

```python
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.modules.data_management import DataManagementModule


class _AudioDataServiceStub:
    def list_column_names(self):
        return [
            "audio_data_id",
            "file_path",
            "product_model",
            "sample_rate",
            "record_date",
            "labels",
            "label_source",
            "barcode",
            "analyse_config",
            "config_content",
        ]

    def list_records(self, _filters=None):
        return [
            {
                "audio_data_id": "a1",
                "file_path": "a.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:00:00+08:00",
                "labels": "OK",
                "label_source": "auto",
                "barcode": "SN-A",
                "analyse_config": "Config",
            },
            {
                "audio_data_id": "a2",
                "file_path": "b.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:01:00+08:00",
                "labels": "NG",
                "label_source": "manual",
                "barcode": "SN-B",
                "analyse_config": "Config",
            },
            {
                "audio_data_id": "a3",
                "file_path": "c.wav",
                "product_model": "M",
                "sample_rate": 44100,
                "record_date": "2026-05-08T10:02:00+08:00",
                "labels": "",
                "label_source": "",
                "barcode": "SN-C",
                "analyse_config": "Config",
            },
        ]

    def list_product_models(self):
        return ["M"]

    def record_date_bounds(self):
        return None, None


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_data_management_displays_label_source_column(qapp):
    services = SimpleNamespace(audio_data_service=_AudioDataServiceStub())
    module = DataManagementModule(services)

    headers = [
        module.table.horizontalHeaderItem(index).text()
        for index in range(module.table.columnCount())
    ]

    assert "判定来源" in headers
    source_column = headers.index("判定来源")
    assert module.table.item(0, source_column).text() == "自动"
    assert module.table.item(1, source_column).text() == "人工"
    assert module.table.item(2, source_column).text() == ""
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_audio_data_service.py tests/test_data_management_module.py -q
```

Expected: FAIL because `label_source` does not exist and data-management has no header/mapping.

- [ ] **Step 3: Implement data model and display**

In `model/audio_data_table.py`:

```python
label_source = TextField(null=True) # 判定来源：auto/manual
```

Place it directly after `labels`.

In `app/service/audio_data_service.py`, update the export model:

```python
label_source = AudioDataTable.label_source.clone()
```

Place it directly after `labels`.

In `ui/modules/data_management.py`:

```python
table_header_labels = {
    ...
    "labels": "标签结果",
    "label_source": "判定来源",
    ...
}
header_min_widths = {
    ...
    "label_source": 90,
    ...
}
```

Add a renderer helper:

```python
@staticmethod
def _display_cell_text(column_name: str, value: object) -> str:
    if column_name == "record_date":
        return format_datetime_display(value)
    if column_name == "label_source":
        text = "" if value is None else str(value).strip()
        if text == "auto":
            return "自动"
        if text == "manual":
            return "人工"
        return text
    return "" if value is None else str(value)
```

Use it in `_render_table` instead of the current inline `record_date` branch.

Do not add `label_source` to filters.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_audio_data_service.py tests/test_data_management_module.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add model/audio_data_table.py app/service/audio_data_service.py ui/modules/data_management.py tests/test_audio_data_service.py tests/test_data_management_module.py
git commit -m "feat(data): show judgment source"
```

## Task 2: Automatic And Manual Row-Level Persistence

**Files:**
- Modify: `app/service/analysis/test_session.py`
- Modify: `tests/test_test_session.py`

- [ ] **Step 1: Write failing `TestSession` tests**

In existing persistence tests around `tests/test_test_session.py`, add expectations:

```python
rec = AudioDataTable.get_by_id(session._audio_data_id)
assert rec.labels == "OK"
assert rec.label_source == "auto"
```

Add manual update tests:

```python
def test_apply_manual_label_overwrites_saved_audio_row_to_manual_ok(tmp_path):
    bus = _make_mock_bus()
    session = TestSession(
        config=_make_config(config_name="Config A"),
        bus=bus,
        ai_service=MagicMock(),
        mic_cal={},
        workorder={"sn": "SN-1", "model": "M"},
        audio_dir=str(tmp_path),
    )
    path = session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)
    AudioDataTable.update(labels="NG", label_source="auto").where(
        AudioDataTable.audio_data_id == session._audio_data_id
    ).execute()

    result = session.apply_manual_label("OK")

    rec = AudioDataTable.get_by_id(session._audio_data_id)
    assert result.success is True
    assert rec.labels == "OK"
    assert rec.label_source == "manual"
    assert rec.file_path == path


def test_apply_manual_label_rejects_missing_audio_data_id(tmp_path):
    bus = _make_mock_bus()
    session = TestSession(
        config=_make_config(),
        bus=bus,
        ai_service=MagicMock(),
        mic_cal={},
        workorder={},
        audio_dir=str(tmp_path),
    )

    result = session.apply_manual_label("OK")

    assert result.success is False
    assert "没有已保存的音频记录" in result.message
```

Add row-missing and invalid-label tests:

```python
def test_apply_manual_label_rejects_missing_database_row(tmp_path):
    bus = _make_mock_bus()
    session = TestSession(
        config=_make_config(),
        bus=bus,
        ai_service=MagicMock(),
        mic_cal={},
        workorder={},
        audio_dir=str(tmp_path),
    )
    session._audio_data_id = "missing-audio-id"

    result = session.apply_manual_label("NG")

    assert result.success is False
    assert "音频记录不存在" in result.message


def test_apply_manual_label_rejects_invalid_label(tmp_path):
    bus = _make_mock_bus()
    session = TestSession(
        config=_make_config(),
        bus=bus,
        ai_service=MagicMock(),
        mic_cal={},
        workorder={},
        audio_dir=str(tmp_path),
    )
    session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)

    result = session.apply_manual_label("BAD")

    assert result.success is False
    assert "无效判定" in result.message
```

Add a real manual NG overwrite assertion:

```python
def test_apply_manual_label_overwrites_saved_audio_row_to_manual_ng(tmp_path):
    bus = _make_mock_bus()
    session = TestSession(
        config=_make_config(config_name="Config A"),
        bus=bus,
        ai_service=MagicMock(),
        mic_cal={},
        workorder={"sn": "SN-1", "model": "M"},
        audio_dir=str(tmp_path),
    )
    session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)
    AudioDataTable.update(labels="OK", label_source="auto").where(
        AudioDataTable.audio_data_id == session._audio_data_id
    ).execute()

    result = session.apply_manual_label("NG")

    rec = AudioDataTable.get_by_id(session._audio_data_id)
    assert result.success is True
    assert rec.labels == "NG"
    assert rec.label_source == "manual"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_test_session.py -q
```

Expected: FAIL because `label_source` is not written and `apply_manual_label` does not exist.

- [ ] **Step 3: Implement persistence result and helpers**

In `app/service/analysis/test_session.py`, add a small dataclass near imports:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ManualLabelUpdateResult:
    success: bool
    message: str = ""
    audio_data_id: str | None = None
```

Add a label normalizer:

```python
@staticmethod
def _normalize_manual_label(label: str) -> str | None:
    text = str(label or "").strip().upper()
    if text in {"OK", "NG"}:
        return text
    return None
```

Update `_persist_audio_result_metadata`:

```python
if label_value is not None:
    update_payload["labels"] = label_value
    update_payload["label_source"] = "auto"
```

Add `apply_manual_label`:

```python
def apply_manual_label(self, label: str) -> ManualLabelUpdateResult:
    normalized = self._normalize_manual_label(label)
    if normalized is None:
        return ManualLabelUpdateResult(False, "无效判定结果")
    if not self._audio_data_id:
        return ManualLabelUpdateResult(False, "当前测试没有已保存的音频记录")
    try:
        updated = (
            AudioDataTable
            .update(labels=normalized, label_source="manual")
            .where(AudioDataTable.audio_data_id == self._audio_data_id)
            .execute()
        )
    except Exception as exc:
        self._bus.log.emit(
            f"[TestSession] WARNING: failed to persist manual judgment "
            f"for audio_data_id={self._audio_data_id}: {exc}"
        )
        return ManualLabelUpdateResult(False, f"保存人工判定失败：{exc}", self._audio_data_id)
    if updated != 1:
        return ManualLabelUpdateResult(False, "当前测试音频记录不存在", self._audio_data_id)
    return ManualLabelUpdateResult(True, "", self._audio_data_id)
```

Keep `_persist_audio_result_metadata` warning behavior for automatic persistence.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_test_session.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_session.py tests/test_test_session.py
git commit -m "feat(factory): persist judgment source on audio rows"
```

## Task 3: TestManager Manual Judgment API

**Files:**
- Modify: `app/service/analysis/test_manager.py`
- Create: `tests/test_test_manager/test_manual_judgment.py`

- [ ] **Step 1: Write failing manager tests**

Create `tests/test_test_manager/test_manual_judgment.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.test_manager import TestManager
from tests.test_test_manager._support import _mock_bus, _mock_services


class _SessionStub:
    def __init__(self, *, run_id="run-1", audio_data_id="audio-1", result=None):
        self._workorder = {"factory_run_id": run_id, "trigger_source": "manual"}
        self._audio_data_id = audio_data_id
        self.apply_manual_label = MagicMock(return_value=result)


class _ManualResult:
    def __init__(self, success, message="", audio_data_id="audio-1"):
        self.success = success
        self.message = message
        self.audio_data_id = audio_data_id


def _make_manager():
    bus = _mock_bus()
    services = _mock_services(bus)
    return TestManager(bus, services), bus


def test_apply_manual_judgment_updates_completed_session():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(True))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("OK")


def test_apply_manual_judgment_allows_saved_no_result_session():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(True), audio_data_id="audio-1")
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="NG")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("NG")


def test_apply_manual_judgment_rejects_missing_session():
    mgr, _bus = _make_manager()

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is False
    assert "没有可判定的测试结果" in result.message


def test_apply_manual_judgment_rejects_stale_run_id():
    mgr, _bus = _make_manager()
    session = _SessionStub(run_id="run-current", result=_ManualResult(True))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-old", label="OK")

    assert result.success is False
    assert "测试结果已过期" in result.message
    session.apply_manual_label.assert_not_called()


def test_apply_manual_judgment_returns_session_failure_message():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(False, "当前测试音频记录不存在"))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is False
    assert result.message == "当前测试音频记录不存在"


def test_apply_manual_judgment_rejects_provided_run_id_when_session_has_no_run_id():
    mgr, _bus = _make_manager()
    session = _SessionStub(run_id="", result=_ManualResult(True))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-from-ui", label="OK")

    assert result.success is False
    assert "测试结果已过期" in result.message
    session.apply_manual_label.assert_not_called()


def test_stopped_partial_saved_session_can_be_manually_judged(monkeypatch):
    from tests.test_test_manager._support import _make_running_factory_manager

    (
        _bus,
        _services,
        mgr,
        session,
        _recorder,
        _monitor,
        _partial_buf,
    ) = _make_running_factory_manager(monkeypatch)
    session.apply_manual_label = MagicMock(return_value=_ManualResult(True))

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is True
    assert mgr._completed_sessions[1] is session

    result = mgr.apply_manual_judgment(config_id=1, run_id=session._workorder["factory_run_id"], label="OK")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("OK")


def test_no_result_after_saved_audio_row_can_be_manually_judged():
    mgr, _bus = _make_manager()
    session = _SessionStub(run_id="run-no-result", audio_data_id="audio-1", result=_ManualResult(True))
    session._config = {"config_id": 101, "selected_channels": [0], "analysis_duration": 1.0}
    session._buffer = np.zeros((100, 1), dtype=np.float32)
    session._sample_rate = 44100.0
    session._audio_file_path = "saved.wav"
    session._run_judgment = MagicMock(side_effect=ValueError("analysis result unavailable"))
    mgr._sessions[101] = session

    mgr._on_session_done(101)

    assert mgr._completed_sessions[101] is session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-no-result", label="NG")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("NG")
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_test_manager/test_manual_judgment.py -q
```

Expected: FAIL because `TestManager.apply_manual_judgment` does not exist.

- [ ] **Step 3: Implement manager API**

In `app/service/analysis/test_manager.py`, import the result type:

```python
from app.service.analysis.test_session import ManualLabelUpdateResult
```

Add helpers:

```python
def apply_manual_judgment(
    self,
    *,
    config_id: int,
    run_id: str | None,
    label: str,
) -> ManualLabelUpdateResult:
    session = self._sessions.get(config_id) or self._completed_sessions.get(config_id)
    if session is None:
        return ManualLabelUpdateResult(False, "当前没有可判定的测试结果")
    if run_id:
        session_run_id = self._factory_run_id(session)
        if not session_run_id or session_run_id != run_id:
            return ManualLabelUpdateResult(False, "当前测试结果已过期，请重新测试")
    apply_manual_label = getattr(session, "apply_manual_label", None)
    if not callable(apply_manual_label):
        return ManualLabelUpdateResult(False, "当前测试不支持人工判定")
    result = apply_manual_label(label)
    if not getattr(result, "success", False):
        message = str(getattr(result, "message", "") or "保存人工判定失败")
        return ManualLabelUpdateResult(False, message, getattr(result, "audio_data_id", None))
    self._bus.log.emit(f"[TestManager] manual judgment saved: config={config_id}, label={label}")
    return ManualLabelUpdateResult(True, "", getattr(result, "audio_data_id", None))
```

Use keyword-only parameters so call sites are readable.

Update `_emit_factory_runtime_stopped` so early-stopped saved sessions remain labelable:

```python
def _emit_factory_runtime_stopped(self, config_id: int, session) -> bool:
    self._analyzing_sessions.discard(config_id)
    self._accepted_factory_stops.discard(config_id)
    completed_session = self._sessions.pop(config_id, None)
    stopped_payload = self._make_factory_runtime_stopped_payload(config_id, session)
    if completed_session is not None and getattr(completed_session, "_audio_data_id", None):
        self._completed_sessions[config_id] = completed_session
    ...
```

Do not store stopped sessions without an `_audio_data_id`; those are not eligible for manual judgment.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_test_manager/test_manual_judgment.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/test_manager.py tests/test_test_manager/test_manual_judgment.py
git commit -m "feat(factory): add manual judgment backend"
```

## Task 4: Factory Controller Manual Judgment State Flow

**Files:**
- Modify: `app/service/factory_mode_controller.py`
- Create: `tests/test_factory_mode_controller/test_manual_judgment.py`

- [ ] **Step 1: Write failing controller tests**

Create `tests/test_factory_mode_controller/test_manual_judgment.py` using `EventBus` and simple service stubs:

```python
from __future__ import annotations

from types import SimpleNamespace

from app.service.factory_mode_controller import FactoryModeController
from app.services_container import EventBus, FactoryControllerState, FactoryReviewPayload


class _ManualResult:
    def __init__(self, success, message="", audio_data_id="audio-1"):
        self.success = success
        self.message = message
        self.audio_data_id = audio_data_id


class _TestStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def is_running(self):
        return False

    def apply_manual_judgment(self, *, config_id, run_id, label):
        self.calls.append((config_id, run_id, label))
        return self.result


def _make_controller(result):
    bus = EventBus()
    services = SimpleNamespace(
        test=_TestStub(result),
        config_service=SimpleNamespace(
            load_config=lambda config_id: {"config_id": config_id, "selected_channels": [0]},
            validate_factory_runtime=lambda config: [],
        ),
    )
    controller = FactoryModeController(bus, services)
    controller.restore_selection((101,), had_missing_configs=False)
    return controller, services, bus


def _enter_review(controller):
    controller._state = FactoryControllerState(
        selected_config_ids=(101,),
        displayed_config_id=101,
        run_phase="reviewing",
        selection_ready=True,
        start_ready=True,
    )
    controller._review_payload = FactoryReviewPayload(
        displayed_config_id=101,
        run_id="run-101",
        trigger_source="manual",
    )


def test_manual_ok_persists_and_resets_review_context():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    states = []
    bus.factory_display_reset.connect(resets.append)
    bus.factory_state_changed.connect(states.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("OK")

    assert result.success is True
    assert services.test.calls == [(101, "run-101", "OK")]
    assert resets == ["manual_judgment_ok"]
    assert controller.state.run_phase == "armed"
    assert controller.state.displayed_config_id is None


def test_manual_ng_uses_ng_reset_reason():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("NG")

    assert result.success is True
    assert services.test.calls == [(101, "run-101", "NG")]
    assert resets == ["manual_judgment_ng"]


def test_manual_judgment_rejects_busy_state_without_reset():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    controller._state = FactoryControllerState(
        selected_config_ids=(101,),
        active_config_id=101,
        displayed_config_id=101,
        run_phase="running",
    )

    result = controller.apply_manual_judgment("OK")

    assert result.success is False
    assert "测试仍在进行中" in result.message
    assert services.test.calls == []
    assert resets == []


def test_manual_judgment_rejects_no_display_context():
    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)

    result = controller.apply_manual_judgment("OK")

    assert result.success is False
    assert "没有可判定的测试结果" in result.message
    assert services.test.calls == []
    assert resets == []


def test_manual_judgment_backend_failure_does_not_reset():
    controller, services, bus = _make_controller(_ManualResult(False, "当前测试音频记录不存在"))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    _enter_review(controller)

    result = controller.apply_manual_judgment("OK")

    assert result.success is False
    assert result.message == "当前测试音频记录不存在"
    assert resets == []
    assert controller.state.run_phase == "reviewing"


def test_stopped_run_preserves_run_id_for_manual_judgment():
    from app.services_container import FactoryRuntimeStoppedPayload

    controller, services, bus = _make_controller(_ManualResult(True))
    resets = []
    bus.factory_display_reset.connect(resets.append)
    controller._state = FactoryControllerState(
        selected_config_ids=(101,),
        active_config_id=101,
        displayed_config_id=101,
        run_phase="running",
        trigger_source="manual",
    )
    controller._owned_runtime_config_id = 101
    controller._owned_runtime_run_id = "run-stop"

    controller.handle_runtime_stopped(
        FactoryRuntimeStoppedPayload(
            config_id=101,
            run_id="run-stop",
            trigger_source="manual",
        )
    )

    result = controller.apply_manual_judgment("OK")

    assert result.success is True
    assert services.test.calls == [(101, "run-stop", "OK")]
    assert resets == ["manual_judgment_ok"]
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_factory_mode_controller/test_manual_judgment.py -q
```

Expected: FAIL because the controller entry point does not exist.

- [ ] **Step 3: Implement controller entry point**

In `app/service/factory_mode_controller.py`, import `ManualLabelUpdateResult` or define a local fallback import:

```python
from app.service.analysis.test_session import ManualLabelUpdateResult
```

Add:

```python
def apply_manual_judgment(self, label: str) -> ManualLabelUpdateResult:
    normalized = str(label or "").strip().upper()
    if normalized not in {"OK", "NG"}:
        return ManualLabelUpdateResult(False, "无效判定结果")
    if self._pending_start is not None or self._state.run_phase in self._BLOCKED_START_PHASES:
        return ManualLabelUpdateResult(False, "测试仍在进行中，无法人工判定")
    config_id = self._state.displayed_config_id
    if config_id is None:
        return ManualLabelUpdateResult(False, "当前没有可判定的测试结果")
    run_id = self._review_payload.run_id
    test_manager = getattr(self.services, "test", None)
    apply_manual_judgment = getattr(test_manager, "apply_manual_judgment", None)
    if not callable(apply_manual_judgment):
        return ManualLabelUpdateResult(False, "当前测试不支持人工判定")
    result = apply_manual_judgment(config_id=config_id, run_id=run_id, label=normalized)
    if not getattr(result, "success", False):
        message = str(getattr(result, "message", "") or "保存人工判定失败")
        return ManualLabelUpdateResult(False, message, getattr(result, "audio_data_id", None))
    reason = "manual_judgment_ok" if normalized == "OK" else "manual_judgment_ng"
    self.clear_for_next_run(reason)
    return ManualLabelUpdateResult(True, "", getattr(result, "audio_data_id", None))
```

Do not call `clear_for_next_run` on failure.

Update `handle_runtime_stopped` to preserve stopped-run ownership before emitting state:

```python
self._review_payload = replace(
    self._review_payload,
    displayed_config_id=payload.config_id,
    run_id=payload.run_id,
    trigger_source=payload.trigger_source,
)
```

This minimal review context is not a chart/result payload; it exists so manual OK/NG can target the stopped run safely.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_factory_mode_controller/test_manual_judgment.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/service/factory_mode_controller.py tests/test_factory_mode_controller/test_manual_judgment.py
git commit -m "feat(factory): route manual judgment through controller"
```

## Task 5: Control Button Feedback

**Files:**
- Modify: `ui/modules/control.py`
- Modify: `tests/test_control_module.py`

- [ ] **Step 1: Write failing control tests**

Update `_FactoryModeStub` in `tests/test_control_module.py`:

```python
self.apply_manual_judgment = MagicMock(return_value=SimpleNamespace(success=True, message=""))
```

Add tests:

```python
def test_manual_pass_click_applies_manual_ok(qapp):
    module, services = _make_module()

    module.btn_pass.click()

    services.factory_mode.apply_manual_judgment.assert_called_once_with("OK")
    assert services.bus.log.emissions[-1][0][0] == "Manual judgment: PASS"


def test_manual_fail_click_applies_manual_ng(qapp):
    module, services = _make_module()

    module.btn_fail.click()

    services.factory_mode.apply_manual_judgment.assert_called_once_with("NG")
    assert services.bus.log.emissions[-1][0][0] == "Manual judgment: FAIL"
```

Add failure-dialog test by monkeypatching `QMessageBox.warning`:

```python
def test_manual_judgment_failure_shows_warning(qapp, monkeypatch):
    module, services = _make_module()
    services.factory_mode.apply_manual_judgment.return_value = SimpleNamespace(
        success=False,
        message="当前没有可判定的测试结果",
    )
    warnings = []
    monkeypatch.setattr(
        "ui.modules.control.QMessageBox.warning",
        lambda parent, title, message: warnings.append((title, message)),
    )

    module.btn_pass.click()

    assert warnings == [("人工判定失败", "当前没有可判定的测试结果")]
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_control_module.py -q
```

Expected: FAIL because buttons still log only.

- [ ] **Step 3: Implement button handler**

In `ui/modules/control.py`, replace log-only lambdas:

```python
self.btn_pass.clicked.connect(lambda: self._apply_manual_judgment("OK"))
self.btn_fail.clicked.connect(lambda: self._apply_manual_judgment("NG"))
```

Add:

```python
def _apply_manual_judgment(self, label: str) -> None:
    normalized = str(label or "").strip().upper()
    log_message = "Manual judgment: PASS" if normalized == "OK" else "Manual judgment: FAIL"
    factory_mode = getattr(self.services, "factory_mode", None)
    apply_manual_judgment = getattr(factory_mode, "apply_manual_judgment", None)
    if not callable(apply_manual_judgment):
        self._show_manual_judgment_warning("当前测试不支持人工判定")
        return
    result = apply_manual_judgment(normalized)
    if not getattr(result, "success", False):
        self._show_manual_judgment_warning(
            str(getattr(result, "message", "") or "保存人工判定失败")
        )
        return
    self.services.bus.log.emit(log_message)

def _show_manual_judgment_warning(self, message: str) -> None:
    QMessageBox.warning(self, "人工判定失败", message)
```

Log after success so failed attempts are not recorded as accepted judgments.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_control_module.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/modules/control.py tests/test_control_module.py
git commit -m "feat(factory-ui): apply manual judgment buttons"
```

## Task 6: SN Reset After Successful Manual Judgment

**Files:**
- Modify: `ui/modules/workorder.py`
- Modify: `tests/test_workorder_module.py`
- Create: `tests/test_video_module.py`

`VideoModule` production behavior is intentionally excluded from this task. The live camera preview is not per-unit test result imagery, so manual judgment reset must not clear, stop, start, refresh, or otherwise disturb it. Add inverse regression coverage in `tests/test_video_module.py` to prove that invariant.

- [ ] **Step 1: Write failing workorder tests**

Update `_BusStub` in `tests/test_workorder_module.py` with:

```python
self.factory_display_reset = _SignalStub()
```

Add:

```python
def test_manual_judgment_reset_clears_sn_and_publishes_blank_payload(qapp):
    module, services = _make_module(saved_selected_ids=(101,))
    module.ed_sn.setText("SN-OLD")
    module.ed_model.setText("MODEL-KEEP")
    services.bus.workorder_changed.emissions.clear()

    services.bus.factory_display_reset.emit("manual_judgment_ok")

    assert module.ed_sn.text() == ""
    assert module.ed_model.text() == "MODEL-KEEP"
    assert module.value()["selected_config_ids"] == (101,)
    assert services.bus.current_workorder_payload == {
        "sn": "",
        "model": "MODEL-KEEP",
        "selected_config_ids": (101,),
    }
    assert services.bus.workorder_changed.emissions[-1][0][0]["sn"] == ""


def test_non_manual_display_reset_preserves_sn(qapp):
    module, services = _make_module(saved_selected_ids=(101,))
    module.ed_sn.setText("SN-KEEP")

    services.bus.factory_display_reset.emit("selection_changed")

    assert module.ed_sn.text() == "SN-KEEP"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_workorder_module.py -q
```

Expected: FAIL because `WorkOrderModule` does not subscribe to reset.

- [ ] **Step 3: Implement workorder reset**

In `ui/modules/workorder.py`, connect in `__init__`:

```python
if hasattr(self.services.bus, "factory_display_reset"):
    self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
```

Add:

```python
def _on_factory_display_reset(self, reason: str) -> None:
    if reason not in {"manual_judgment_ok", "manual_judgment_ng"}:
        return
    if self.ed_sn.text() == "":
        self._publish_workorder(force=True)
        return
    self.ed_sn.clear()
    self._publish_workorder(force=True)
```

Guard against duplicate emissions if `textChanged` already published; tests should accept at least the final emission with blank SN.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_workorder_module.py -q
```

Expected: PASS.

- [ ] **Step 4a: Write video non-reset regression test**

Create `tests/test_video_module.py` with a fake `CameraThread`, a local `ui.modules.video.cv2` monkeypatch, and a bus exposing `factory_display_reset` and `log`. Instantiate `VideoModule`, render a frame with `module.update_frame(frame)`, then emit both `manual_judgment_ok` and `manual_judgment_ng`.

Assert the preview remains populated, text is not changed to `暂无画面`, `module.camera_thread` is the same object, the fake thread was not stopped, and no second fake thread was created or started.

- [ ] **Step 4b: Run video regression test**

Run:

```bash
pytest tests/test_video_module.py -q
```

Expected: PASS. The same test should fail against the old reset-clearing implementation.

- [ ] **Step 5: Commit**

```bash
git add ui/modules/workorder.py tests/test_workorder_module.py tests/test_video_module.py
git commit -m "feat(factory-ui): clear per-unit state after manual judgment"
```

## Task 7: Integrated Manual Judgment Flow And Full Verification

**Files:**
- Modify only files needed to fix integration failures from previous tasks.
- Prefer adding focused regression tests instead of broad brittle UI assertions.

- [ ] **Step 1: Run targeted feature tests**

Run:

```bash
pytest tests/test_audio_data_service.py tests/test_data_management_module.py tests/test_test_session.py tests/test_test_manager/test_manual_judgment.py tests/test_factory_mode_controller/test_manual_judgment.py tests/test_control_module.py tests/test_workorder_module.py tests/test_video_module.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader related tests**

Run:

```bash
pytest tests/test_db_connection_service.py tests/test_service_container.py tests/test_test_manager tests/test_factory_mode_controller tests/test_main_window.py -q
```

Expected: PASS.

- [ ] **Step 3: Fix any related failures**

If failures appear:

- Fix only behavior touched by this feature.
- Do not add old-database migration/backfill for `label_source`.
- Do not reintroduce deleted non-owned main-window assertions.
- Do not create a second audio data row for manual judgment.

- [ ] **Step 4: Run full suite**

Run:

```bash
pytest
```

Expected: PASS.

- [ ] **Step 5: Commit final fixes if any**

If Task 7 required changes:

```bash
git add <changed-files>
git commit -m "test(factory): verify manual judgment source flow"
```

If no changes were needed, do not create an empty commit.

## Execution Notes

- Use `subagent-driven-development` for implementation.
- Execute tasks sequentially.
- After each implementation task, run paired `spec-code-reviewer` and `quality-code-reviewer` reviews with the spec gate.
- Do not proceed to the next task until the current task is implemented, reviewed, and accepted or explicitly overruled with a technical reason.
- Keep commits small and aligned to the task boundaries above.
