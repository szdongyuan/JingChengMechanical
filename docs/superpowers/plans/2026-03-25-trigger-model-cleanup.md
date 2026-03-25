# Trigger Model Cleanup Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `trigger_type`, add minimal persisted PLC metadata, and leave the branch with one explicit trigger model for scan and PLC capability.

**Architecture:** Keep this cleanup narrow. `TestConfigTable` owns the persisted trigger-related fields, `ConfigService` owns PLC metadata normalization and CRUD persistence, and `ConfigModule` owns plain editing controls. Do not introduce factory-runtime APIs or compatibility layers in this plan.

**Tech Stack:** Python, PyQt6, peewee/SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-03-25-trigger-model-cleanup-design.md`

**Authority Note:** For trigger-model work, this plan supersedes `docs/superpowers/plans/2026-03-20-factory-mode-analysis-ui.md`.

---

## File Map

| File | Responsibility |
|------|----------------|
| Modify: `model/test_config_table.py` | Remove `trigger_type`; add normalized PLC metadata fields on the config model |
| Modify: `app/service/db_connection_service.py` | Stop adding `trigger_type` during startup migration; keep fresh-schema startup compatible with the new model |
| Modify: `app/service/config_service.py` | Remove `trigger_type` from load/save payloads; normalize/save/load `plc_device_key` and `plc_device_label` |
| Modify: `ui/modules/config.py` | Replace trigger selector UI with plain PLC metadata inputs; remove `trigger_type` collection/population |
| Modify: `tests/test_test_config_table.py` | Assert PLC field defaults and absence of `trigger_type` |
| Modify: `tests/test_db_connection_service.py` | Assert fresh startup schema includes PLC metadata and no `trigger_type`; assert existing compatibility test no longer expects `trigger_type` |
| Modify: `tests/test_config_service.py` | Assert PLC metadata round-trip, whitespace normalization, and absence of `trigger_type` in loaded payloads |
| Create: `tests/test_config_module.py` | Assert config UI exposes PLC inputs, omits trigger selector, and collects PLC metadata without `trigger_type` |

## Chunk 1: Persisted Trigger Model

### Task 1: Remove `trigger_type` from the model and startup schema expectations

**Files:**
- Modify: `model/test_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `tests/test_test_config_table.py`
- Modify: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write the failing model and DB-startup tests**

Add tests that make the new trigger-model contract explicit:

```python
def test_model_defines_plc_metadata_fields_without_trigger_type():
    assert "trigger_type" not in ConfigTable._meta.fields
    assert "plc_device_key" in ConfigTable._meta.fields
    assert "plc_device_label" in ConfigTable._meta.fields

    config = ConfigTable.create(config_name="Trigger Cleanup")

    assert config.plc_device_key is None
    assert config.plc_device_label is None
```

```python
def test_connect_creates_test_config_table_with_plc_metadata_fields(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        tmp_db = SqliteDatabase(db_path, pragmas={"foreign_keys": 1})
        import model.base_mode as base_mod
        monkeypatch.setattr(base_mod, "db", tmp_db)
        from app.service.db_connection_service import DbConnectionService
        import app.service.db_connection_service as svc_mod
        monkeypatch.setattr(svc_mod, "db", tmp_db)
        from model.channel_config_table import ChannelConfigTable
        from model.test_config_table import TestConfigTable

        tmp_db.bind([TestConfigTable, ChannelConfigTable, AiToolchainTable, AiBindingTable, AiExecutionTable])
        DbConnectionService().connect()
        cursor = tmp_db.execute_sql("PRAGMA table_info(test_config_table)")
        column_names = {row[1] for row in cursor.fetchall()}

        assert "trigger_type" not in column_names
        assert "plc_device_key" in column_names
        assert "plc_device_label" in column_names
    finally:
        tmp_db.close()
        os.unlink(db_path)
```

Also rewrite the existing assertions in `tests/test_test_config_table.py` so the file no longer references `trigger_type` at all, and update the existing existing-DB compatibility test so it asserts:

- the existing input/output/selected-channel compatibility columns are still added
- `trigger_type` is not added by `_migrate()`
- `plc_device_key` is not added by `_migrate()`
- `plc_device_label` is not added by `_migrate()`

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
python -m pytest tests/test_test_config_table.py tests/test_db_connection_service.py -v
```

Expected: FAIL because the model still defines `trigger_type`, fresh startup schema does not expose PLC metadata fields, and the DB startup code still adds `trigger_type`.

- [ ] **Step 3: Implement the minimal schema and startup changes**

Make only these changes:

```python
class TestConfigTable(BaseModel):
    scan_triggers_test = BooleanField(default=False)
    plc_device_key = TextField(null=True, default=None)
    plc_device_label = TextField(null=True, default=None)
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)
    input_device_name = TextField(null=True)
```

Delete this line outright:

```python
trigger_type = TextField(default='manual')
```

In `DbConnectionService._migrate()`:

```python
columns = [
    ("input_device_name", "TEXT"),
    ("input_device_hostapi", "TEXT"),
    ("input_device_samplerate", "REAL"),
    ("output_device_name", "TEXT"),
    ("output_device_hostapi", "TEXT"),
    ("output_device_samplerate", "REAL"),
    ("selected_channels", "TEXT DEFAULT '[]'"),
]
```

Do not add `plc_device_key` / `plc_device_label` to `_migrate()` in this cleanup. Fresh schema creation from the updated model is the supported path.

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
python -m pytest tests/test_test_config_table.py tests/test_db_connection_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add model/test_config_table.py app/service/db_connection_service.py tests/test_test_config_table.py tests/test_db_connection_service.py
git commit -m "refactor(config): remove trigger_type from config schema"
```

### Task 2: Normalize and persist PLC metadata in `ConfigService`

**Files:**
- Modify: `app/service/config_service.py`
- Modify: `tests/test_config_service.py`

- [ ] **Step 1: Write the failing service tests**

Add tests for the new service contract:

```python
def test_load_config_returns_plc_metadata_without_trigger_type():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        plc_device_key="PLC-A",
        plc_device_label="Line 1",
        selected_channels="[]",
    )

    result = svc.load_config(cfg.config_id)

    assert "trigger_type" not in result
    assert result["plc_device_key"] == "PLC-A"
    assert result["plc_device_label"] == "Line 1"
```

```python
def test_save_config_normalizes_plc_metadata():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "plc_device_key": "  PLC-A  ",
            "plc_device_label": "  Line 1  ",
            "selected_channels": [],
            "channels": [],
        }
    )

    loaded = svc.load_config(config_id)
    assert loaded["plc_device_key"] == "PLC-A"
    assert loaded["plc_device_label"] == "Line 1"

    row = ConfigTable.get_by_id(config_id)
    assert row.plc_device_key == "PLC-A"
    assert row.plc_device_label == "Line 1"
```

```python
def test_save_config_blank_plc_metadata_persists_none():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "plc_device_key": "   ",
            "plc_device_label": "   ",
            "selected_channels": [],
            "channels": [],
        }
    )

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)
    assert loaded["plc_device_key"] is None
    assert loaded["plc_device_label"] is None
    assert row.plc_device_key is None
    assert row.plc_device_label is None
```

Also rewrite the current `trigger_type`-based tests in `tests/test_config_service.py` so the file contains no `trigger_type` fixtures or assertions after this task.

Preserve the existing hardware-device and `selected_channels` assertions while rewriting those tests so this cleanup does not reduce coverage of unchanged persistence behavior.

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
python -m pytest tests/test_config_service.py -v
```

Expected: FAIL because `ConfigService` still reads and writes `trigger_type` and does not expose PLC metadata.

- [ ] **Step 3: Implement the minimal `ConfigService` changes**

Add a focused normalization helper and use it only for PLC metadata persistence:

```python
@staticmethod
def _normalize_optional_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
```

Update `load_config()` and `save_config()` so they:

- omit `trigger_type`
- include `plc_device_key`
- include `plc_device_label`
- persist normalized `None` / trimmed string values only

Do not add any new runtime-validation method in this task.

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
python -m pytest tests/test_config_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/service/config_service.py tests/test_config_service.py
git commit -m "feat(config): persist normalized plc metadata"
```

## Chunk 2: Config UI and Cleanup Verification

### Task 3: Replace the trigger selector with plain PLC metadata inputs

**Files:**
- Modify: `ui/modules/config.py`
- Create: `tests/test_config_module.py`

- [ ] **Step 1: Write the failing config-module tests**

Create `tests/test_config_module.py` with lightweight UI tests built around fake services:

```python
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ui.modules.config import ConfigModule


class _FakeBus(QObject):
    refresh_channels = pyqtSignal()
    status = pyqtSignal(str)


def _make_module():
    app = QApplication.instance() or QApplication([])
    services = _FakeServices()
    module = ConfigModule(services)
    return app, module


class _FakeConfigService:
    def list_configs(self):
        return []


class _FakeHardwareService:
    def query_devices_and_hostapis(self, refresh=False):
        return (
            [
                {
                    "name": "Mic 1",
                    "hostapi": 0,
                    "default_samplerate": 48000,
                    "max_input_channels": 2,
                    "max_output_channels": 0,
                },
                {
                    "name": "Speaker 1",
                    "hostapi": 0,
                    "default_samplerate": 44100,
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                },
            ],
            [{"name": "ASIO"}],
        )


class _FakeAiService:
    def list_enabled_toolchains(self):
        return []

    def get_toolchain(self, toolchain_id):
        return None

    def resolve_binding(self, config_id):
        return None


class _FakeServices:
    def __init__(self):
        self.bus = _FakeBus()
        self.config_service = _FakeConfigService()
        self.hss_service = _FakeHardwareService()
        self.ai_service = _FakeAiService()


def test_collect_data_includes_plc_metadata_fields():
    _, module = _make_module()
    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.txt_plc_device_key.setText("  PLC-A  ")
    module.txt_plc_device_label.setText("  Line 1  ")

    data = module._collect_data()

    assert set(data.keys()) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "scan_triggers_test",
        "plc_device_key",
        "plc_device_label",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "channels",
    }
    assert data["plc_device_key"] == "  PLC-A  "
    assert data["plc_device_label"] == "  Line 1  "
```

```python
def test_populate_hardware_card_loads_plc_metadata_fields():
    _, module = _make_module()
    module._populate_hardware_card(
        {
            "plc_device_key": "PLC-A",
            "plc_device_label": "Line 1",
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "output_device_name": None,
            "output_device_hostapi": None,
            "output_device_samplerate": None,
            "selected_channels": [],
        }
    )

    assert module.txt_plc_device_key.text() == "PLC-A"
    assert module.txt_plc_device_label.text() == "Line 1"
    assert module.chk_scan is not None
    assert not hasattr(module, "combo_trigger")
```

```python
def test_populate_hardware_card_handles_unbound_plc_metadata():
    _, module = _make_module()
    module.txt_plc_device_key.setText("stale")
    module.txt_plc_device_label.setText("stale")

    module._populate_hardware_card(
        {
            "plc_device_key": None,
            "plc_device_label": None,
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "output_device_name": None,
            "output_device_hostapi": None,
            "output_device_samplerate": None,
            "selected_channels": [],
        }
    )

    assert module.txt_plc_device_key.text() == ""
    assert module.txt_plc_device_label.text() == ""
```

```python
def test_scan_checkbox_behavior_is_preserved_in_collect_and_populate():
    _, module = _make_module()
    module._current_config_id = 7
    module.chk_scan.setChecked(True)

    data = module._collect_data()
    assert data["scan_triggers_test"] is True

    module.chk_scan.setChecked(False)
    module._populate_detail(
        {
            "config_id": 7,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": True,
            "plc_device_key": None,
            "plc_device_label": None,
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "output_device_name": None,
            "output_device_hostapi": None,
            "output_device_samplerate": None,
            "selected_channels": [],
            "channels": [],
        }
    )

    assert module.chk_scan.isChecked() is True
```

```python
def test_hardware_card_still_round_trips_devices_and_selected_channels():
    _, module = _make_module()
    module._populate_hardware_card(
        {
            "plc_device_key": "PLC-A",
            "plc_device_label": "Line 1",
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "ASIO",
            "output_device_samplerate": 44100,
            "selected_channels": [0, 1],
        }
    )

    data = module._collect_data()

    assert data["input_device_name"] == "Mic 1"
    assert data["input_device_hostapi"] == "ASIO"
    assert data["input_device_samplerate"] == 48000
    assert data["output_device_name"] == "Speaker 1"
    assert data["output_device_hostapi"] == "ASIO"
    assert data["output_device_samplerate"] == 44100
    assert data["selected_channels"] == [0, 1]
    assert data["plc_device_key"] == "PLC-A"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
python -m pytest tests/test_config_module.py -v
```

Expected: FAIL because the module still exposes `combo_trigger` and does not have PLC key/label inputs.

- [ ] **Step 3: Implement the minimal UI changes**

In `ui/modules/config.py`, replace the trigger combo with two plain text inputs:

```python
self.txt_plc_device_key = QLineEdit()
hw_lay.addRow("PLC Key", self.txt_plc_device_key)

self.txt_plc_device_label = QLineEdit()
hw_lay.addRow("PLC 显示名", self.txt_plc_device_label)
```

Update `_populate_hardware_card()` and `_collect_data()` so they:

- never read or write `trigger_type`
- populate `txt_plc_device_key` and `txt_plc_device_label`
- keep the existing scan checkbox, device selectors, and channel selection behavior unchanged

Let `ConfigService` continue to own normalization. The UI may collect raw text.

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
python -m pytest tests/test_config_module.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/modules/config.py tests/test_config_module.py
git commit -m "feat(config-ui): replace trigger selector with plc metadata fields"
```

### Task 4: Verify repo cleanup and active authority

**Files:**
- No code changes expected
- Test: `tests/test_test_config_table.py`
- Test: `tests/test_db_connection_service.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Run the targeted cleanup suite**

Run:

```bash
python -m pytest tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py -v
```

Expected: PASS.

- [ ] **Step 2: Verify code and tests no longer mention `trigger_type`**

Run:

```bash
rg -n "trigger_type" . -S --glob "!docs/**" --glob "!.agents/**" --glob "!.git/**"
```

Expected: no output.

- [ ] **Step 3: Verify the docs authority boundaries**

Run:

```bash
rg -n "trigger_type" docs -S
Get-Content docs/superpowers/plans/2026-03-25-trigger-model-cleanup.md | Where-Object { $_ -notmatch "Select-String|Get-Content" } | Select-String -Pattern "^\*\*Authority Note:\*\*.*2026-03-20-factory-mode-analysis-ui.md"
Get-Content docs/superpowers/plans/2026-03-25-trigger-model-cleanup.md | Where-Object { $_ -notmatch "Select-String|Get-Content" } | Select-String -Pattern "trigger_type == 'plc'","trigger_type.*eligib","trigger_type.*route"
Select-String -Path docs/superpowers/plans/2026-03-20-factory-mode-analysis-ui.md -Pattern "trigger_type == 'plc'","trigger_type.*eligib","trigger_type.*route"
```

Expected: only these file paths may appear in output:

- `docs/superpowers/specs/2026-03-17-per-config-hardware-trigger-design.md`
- `docs/superpowers/specs/2026-03-20-factory-mode-analysis-ui-design.md`
- `docs/superpowers/specs/2026-03-25-trigger-model-cleanup-design.md`
- `docs/superpowers/plans/2026-03-20-factory-mode-analysis-ui.md`
- `docs/superpowers/plans/2026-03-25-trigger-model-cleanup.md`

And for the follow-up commands:

- the authority-note check returns exactly one match in the March 25 cleanup plan
- no matches from the filtered March 25 plan content
- any trigger-type-based routing or eligibility matches may only come from the superseded March 20 historical plan

- [ ] **Step 4: Fix unexpected matches and rerun verification until clean**

If Step 2 or Step 3 returns unexpected matches:

- edit the offending files
- rerun Step 1
- rerun Step 2
- rerun Step 3
- do not proceed until the targeted pytest suite passes and both search steps match the expected clean outputs

- [ ] **Step 5: Confirm worktree state after verification**

```bash
git status
```

Expected:

- if no follow-up edits were needed, there are no new unexpected changes from Task 4
- if follow-up edits were needed, only the intentional cleanup files from Task 4 remain modified

- [ ] **Step 6: Commit follow-up cleanup only if Step 4 changed files**

If Step 4 required edits:

```bash
git status --short
git commit -m "chore(trigger): clean remaining trigger_type references"
```

Expected:

- before running the commit, explicitly stage only the Step 4 cleanup paths shown by `git status --short` with a `git add --` command that names each path directly
- no unrelated worktree changes are staged
- one cleanup commit exists only when Task 4 required follow-up file edits

---

Plan complete and saved to `docs/superpowers/plans/2026-03-25-trigger-model-cleanup.md`. Ready to execute?
