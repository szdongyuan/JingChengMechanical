# Manual NG Categories Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每条测试配置增加有序的手动 NG 类别列表，在手工 NG 前按需强制选择类别，并把类别与人工 NG 原子写入同一条测试记录，同时保持自动判定、手工 OK、页面重置和 PLC 行为不变。

**Architecture:** 使用一个纯领域模块统一类别规范化、严格 JSON 解析、完整有序列表指纹和选择上下文，避免配置服务、控制器和测试管理器各自实现不同规则。配置 UI 与运行时选择 UI 分离：配置页使用可排序列表编辑器，测试页使用单选对话框；运行时从控制器捕获 `config_id + run_id + SHA-256` 上下文，并由 `TestManager` 在共享 SQLite 事务中复核配置指纹后调用 `TestSession` 原子更新音频记录。

**Tech Stack:** Python 3.11、PyQt6、Peewee、SQLite、pytest

---

## Authoritative Context

- Spec: `docs/double-sdd/specs/2026-07-24-manual-ng-categories-design.md`
- Execute tasks sequentially from a dedicated `.worktrees/...` worktree.
- After every task, run the listed focused verification and commit only that task's files.
- Do not modify the user's unrelated existing change in `base/utils.py`.
- The spec has no UI design reference. Do not create or depend on a design-reference artifact.
- Backward compatibility is intentionally not required:
  - package format v1 must be rejected after v2 is introduced;
  - prepared databases missing either new column must fail startup;
  - no runtime migration or shipped migration script may be added.

## File and Responsibility Map

### New files

- `app/service/manual_ng_categories.py`
  - Pure category-name validation and normalization.
  - Strict JSON encode/decode.
  - Canonical ordered-list SHA-256 fingerprint.
  - Immutable selection-context and preparation-result value objects.
  - Submission validation shared by controller and manager.
- `ui/widgets/manual_ng_category_list_editor.py`
  - Reusable ordered list editor for add, rename, delete, move up, and move down.
  - UI-level validation and `categories_changed` signal.
- `ui/popups/manual_ng_category_dialog.py`
  - Modal single-choice dialog with no initial selection.
  - Confirm disabled until the user actively selects one category.
- `tests/test_manual_ng_categories.py`
  - Pure domain rules and deterministic fingerprint tests.
- `tests/test_manual_ng_category_list_editor.py`
  - Ordered-list editor interaction tests.
- `tests/test_manual_ng_category_dialog.py`
  - Runtime single-choice dialog tests.

### Existing files to modify

- `model/test_config_table.py`
  - Add `manual_ng_categories`.
- `model/audio_data_table.py`
  - Add `manual_ng_category`.
- `app/service/db_connection_service.py`
  - Require both columns in file-backed startup schema checks.
- `app/service/config_service.py`
  - Load, validate, save, import, and expose strict manual NG categories.
- `ui/modules/config.py`
  - Register the top-level category card and include it in collect/populate/dirty state.
- `app/service/config_package_service.py`
  - Raise package `FORMAT_VERSION` to 2 and require the category field before side effects.
- `app/service/analysis/test_session.py`
  - Clear the category for automatic judgment and manual OK; persist it for manual NG.
- `app/service/analysis/test_manager.py`
  - Resolve the exact completed session and verify the category context inside the database transaction.
- `app/service/factory_mode_controller.py`
  - Capture immutable selection context, reject display identity drift, and preserve existing reset behavior.
- `ui/modules/control.py`
  - Add category preparation/dialog flow only to manual NG.
- `app/service/audio_data_service.py`
  - Include the category in exported SQLite databases.
- `ui/modules/data_management.py`
  - Show the `手动NG类别` column.

### Existing tests to extend

- `tests/test_db_connection_service.py`
- `tests/test_config_service.py`
- `tests/test_config_module.py`
- `tests/test_config_package_service.py`
- `tests/test_test_session.py`
- `tests/test_test_manager/test_manual_judgment.py`
- `tests/test_factory_mode_controller/test_manual_judgment.py`
- `tests/test_control_module.py`
- `tests/test_audio_data_service.py`
- `tests/test_data_management_module.py`

## Interface Contracts Locked by This Plan

The implementation should converge on these interfaces. Minor private naming changes are acceptable only if behavior and task boundaries remain identical.

```python
# app/service/manual_ng_categories.py
@dataclass(frozen=True)
class ManualNgSelectionContext:
    config_id: int
    run_id: str
    categories: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class ManualNgSelectionPreparation:
    success: bool
    message: str = ""
    context: ManualNgSelectionContext | None = None


def normalize_manual_ng_categories(value: object) -> list[str]: ...
def normalize_manual_ng_category_name(value: object) -> str: ...
def decode_manual_ng_categories(raw_json: object) -> list[str]: ...
def encode_manual_ng_categories(value: object) -> str: ...
def manual_ng_categories_fingerprint(categories: object) -> str: ...
def build_manual_ng_selection_context(
    *, config_id: int, run_id: str, categories: object
) -> ManualNgSelectionContext: ...
def validate_manual_ng_submission(
    *,
    context: ManualNgSelectionContext,
    current_categories: object,
    selected_category: object,
) -> str | None: ...
```

```python
# app/service/config_service.py
def load_manual_ng_categories(self, config_id: int) -> list[str]: ...
```

```python
# app/service/factory_mode_controller.py
def prepare_manual_ng_judgment(self) -> ManualNgSelectionPreparation: ...

def apply_manual_judgment(
    self,
    label: str,
    *,
    selection_context: ManualNgSelectionContext | None = None,
    manual_ng_category: str | None = None,
) -> ManualLabelUpdateResult: ...
```

```python
# app/service/analysis/test_manager.py
def apply_manual_judgment(
    self,
    *,
    config_id: int,
    run_id: str | None,
    label: str,
    selection_context: ManualNgSelectionContext | None = None,
    manual_ng_category: str | None = None,
) -> ManualLabelUpdateResult: ...
```

```python
# app/service/analysis/test_session.py
def apply_manual_label(
    self,
    label: str,
    *,
    manual_ng_category: str | None = None,
) -> ManualLabelUpdateResult: ...
```

### Task 1: Add Schema Fields and File-Database Startup Gates

**Files:**
- Modify: `model/test_config_table.py:15-64`
- Modify: `model/audio_data_table.py:6-25`
- Modify: `app/service/db_connection_service.py:24-240`
- Modify: `tests/test_db_connection_service.py:1-765`

- [ ] **Step 1: Write failing ORM field tests**

Add focused assertions to `tests/test_db_connection_service.py` or a small schema test section:

```python
from model.audio_data_table import AudioDataTable
from model.test_config_table import TestConfigTable


def test_manual_ng_schema_fields_are_declared():
    assert TestConfigTable.manual_ng_categories.default == "[]"
    assert TestConfigTable.manual_ng_categories.null is False
    assert AudioDataTable.manual_ng_category.null is True
```

Also create the models in a temporary SQLite database and assert `PRAGMA table_info(test_config_table)` reports `manual_ng_categories` as `TEXT NOT NULL DEFAULT '[]'`, so the model-created test schema matches the prepared artifact contract rather than relying only on Peewee's application-side default.

- [ ] **Step 2: Extend prepared-database fixtures and add missing-column failures**

Add the expected DDL fragments:

```python
TEST_CONFIG_COLUMN_SQL["manual_ng_categories"] = "TEXT NOT NULL DEFAULT '[]'"
AUDIO_DATA_COLUMN_SQL = {
    "manual_ng_category": "TEXT DEFAULT NULL",
}
```

Update the prepared-database helper so its complete schema includes both columns. Add two independent tests:

```python
def test_connect_rejects_missing_manual_ng_categories_without_mutating(...):
    ...
    with pytest.raises(DatabaseStartupError, match="manual_ng_categories"):
        service.connect()
    assert _table_column_names(tmp_db, "test_config_table") == before


def test_connect_rejects_missing_manual_ng_category_without_mutating(...):
    ...
    with pytest.raises(DatabaseStartupError, match="manual_ng_category"):
        service.connect()
    assert _table_column_names(tmp_db, "audio_data_table") == before
```

Also extend the existing “prepared database accepted” test so it proves a file database containing both columns starts successfully.

- [ ] **Step 3: Run the schema tests and confirm RED**

Run:

```powershell
pytest tests/test_db_connection_service.py -q
```

Expected: FAIL because the ORM fields and required audio/config column gates do not exist.

- [ ] **Step 4: Add the Peewee fields**

Add:

```python
# model/test_config_table.py
manual_ng_categories = TextField(
    default="[]",
    constraints=[SQL("DEFAULT '[]'")],
)

# model/audio_data_table.py
manual_ng_category = TextField(null=True, default=None)
```

Place `manual_ng_categories` with other test-level settings and `manual_ng_category` immediately after `label_source`, so model and exported-table column order remain predictable.
Import `SQL` from Peewee in `model/test_config_table.py`; the SQL constraint is required because Peewee's `default=` alone is application-side and does not create the database `DEFAULT` clause.

- [ ] **Step 5: Add the startup gates**

In `DbConnectionService`:

```python
_REQUIRED_TEST_CONFIG_COLUMNS = frozenset({
    ...,
    "manual_ng_categories",
})

_REQUIRED_AUDIO_DATA_COLUMNS = frozenset({"manual_ng_category"})
```

Call `_ensure_required_table_columns("audio_data_table", self._REQUIRED_AUDIO_DATA_COLUMNS)` from `_ensure_required_runtime_columns()`.

Do not add `ALTER TABLE`, auto-create, defaults for missing file schemas, or any runtime repair behavior.

- [ ] **Step 6: Run focused verification and confirm GREEN**

Run:

```powershell
pytest tests/test_db_connection_service.py -q
```

Expected: all tests in the file PASS, including the two independent missing-column failures and the complete prepared-schema success case.

- [ ] **Step 7: Commit Task 1**

```powershell
git add model/test_config_table.py model/audio_data_table.py app/service/db_connection_service.py tests/test_db_connection_service.py
git commit -m "feat: require manual NG database schema"
```

### Task 2: Implement Category Domain Rules and Config-Service Persistence

**Files:**
- Create: `app/service/manual_ng_categories.py`
- Create: `tests/test_manual_ng_categories.py`
- Modify: `app/service/config_service.py:179-2100`
- Modify: `tests/test_config_service.py:1-end`
- Modify: `tests/test_config_package_service.py:53-125` only to keep legacy-schema fixtures focused on their original omitted columns

- [ ] **Step 1: Write failing pure-domain tests**

Create `tests/test_manual_ng_categories.py` with table-driven coverage:

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([], []),
        ([" 异响 ", "漏装"], ["异响", "漏装"]),
    ],
)
def test_normalize_manual_ng_categories(raw, expected):
    assert normalize_manual_ng_categories(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "异响",
        [1],
        [""],
        ["   "],
        ["异响", " 异响 "],
        ["异响\n漏装"],
        ["\n异响"],
        ["异响\n"],
        ["异响\t漏装"],
        ["\t异响"],
        ["异响\t"],
        ["异响\u2028漏装"],
        ["异响\u0000"],
        ["异响\ud800"],
    ],
)
def test_normalize_manual_ng_categories_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        normalize_manual_ng_categories(raw)
```

Include positive cases containing a private-use character (`Co`) and a format character (`Cf`) so an implementation that rejects every Unicode `C*` category fails the test. Add explicit normalization, strict-JSON decode, and fingerprint tests for an isolated surrogate such as `"\ud800"`; all must raise `ValueError`, never leak `UnicodeEncodeError`.

Add strict decode tests for malformed JSON, non-list JSON, and duplicate/control-character content. Add fingerprint tests proving:

- same normalized ordered list produces the same SHA-256;
- rename, addition, removal, and reordering each change the fingerprint;
- the digest equals SHA-256 of UTF-8 canonical JSON generated with `ensure_ascii=False` and `separators=(",", ":")`.

- [ ] **Step 2: Run pure-domain tests and confirm RED**

Run:

```powershell
pytest tests/test_manual_ng_categories.py -q
```

Expected: collection/import FAIL because `app.service.manual_ng_categories` does not exist.

- [ ] **Step 3: Implement the pure domain module**

Use one canonical name validator. It must inspect the original string before trimming, reject Unicode general categories `Cc` and `Cs` plus line/paragraph separators (`Zl` and `Zp`), and must not reject unrelated `Cf`, `Co`, or `Cn` characters merely because their category starts with `C`. `Cs` is rejected separately because isolated surrogates are not Unicode scalar values and cannot be encoded by the canonical UTF-8 fingerprint path. Only after forbidden-character validation may it trim surrounding Unicode whitespace.

Core implementation shape:

```python
def normalize_manual_ng_category_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("手动 NG 类别名称必须为文本")
    if any(
        unicodedata.category(ch) in {"Cc", "Cs", "Zl", "Zp"}
        for ch in value
    ):
        raise ValueError(
            "手动 NG 类别名称不能包含控制字符、换行符或代理字符"
        )
    name = value.strip()
    if not name:
        raise ValueError("手动 NG 类别名称不能为空")
    return name


def normalize_manual_ng_categories(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("手动 NG 类别必须为列表")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in value:
        name = normalize_manual_ng_category_name(raw_name)
        if name in seen:
            raise ValueError(f"手动 NG 类别「{name}」重复")
        seen.add(name)
        normalized.append(name)
    return normalized


def decode_manual_ng_categories(raw_json: object) -> list[str]:
    if not isinstance(raw_json, str):
        raise ValueError("手动 NG 类别配置损坏")
    try:
        decoded = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("手动 NG 类别配置损坏") from exc
    return normalize_manual_ng_categories(decoded)


def manual_ng_categories_fingerprint(categories: object) -> str:
    normalized = normalize_manual_ng_categories(categories)
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
```

Implement the immutable value objects and `validate_manual_ng_submission()` now, even though manager/controller usage comes in Tasks 6 and 7. The validator must:

- reject context/current-list fingerprint mismatch;
- require the submitted string to match one normalized configured item byte-for-byte when the current list is non-empty;
- never trim or otherwise normalize the submitted selection into membership (for example, `" 异响 "` must not match `"异响"`);
- require `None`/empty selection when the current list is empty;
- return the normalized chosen name or `None`.

- [ ] **Step 4: Run pure-domain tests and confirm GREEN**

Run:

```powershell
pytest tests/test_manual_ng_categories.py -q
```

Expected: all pure-domain tests PASS.

- [ ] **Step 5: Write failing ConfigService tests**

Extend `_config_payload()` with:

```python
manual_ng_categories: list[str] | None = None
...
"manual_ng_categories": (
    [] if manual_ng_categories is None else manual_ng_categories
),
```

Add tests for:

- `create_config()` persists `"[]"`;
- save/load round-trip preserves order and trims names;
- invalid list type, non-string, blank, duplicate, and control characters are rejected by `validate()`/`save_config()`;
- ordinary save and direct import-service payloads missing `manual_ng_categories` are rejected instead of defaulting to `[]`;
- malformed persisted JSON causes `load_config()` and `load_manual_ng_categories()` to raise a visible `ValueError`, not silently return `[]`;
- `prepare_import_payload()` carries the category list;
- `load_manual_ng_categories(config_id)` returns the same normalized list as `load_config()`.

Update legacy-schema SQL helpers in `tests/test_config_service.py` and `tests/test_config_package_service.py` to include:

```sql
manual_ng_categories TEXT NOT NULL DEFAULT '[]'
```

Those helpers may continue omitting monitor/current-meter columns for their original test purpose; they must not accidentally test a database now declared unsupported.

- [ ] **Step 6: Run ConfigService tests and confirm RED**

Run:

```powershell
pytest tests/test_config_service.py -q
```

Expected: new tests FAIL because the service neither loads nor saves the category list.

- [ ] **Step 7: Implement ConfigService support**

Make these exact changes:

1. Add `"manual_ng_categories"` to `_CONFIG_LOAD_FIELD_NAMES`.
2. In `load_config()`, strictly decode the database value and include the normalized list in the returned dict.
3. In `create_config()`, explicitly include the encoded empty list even though the DB also has a default.
4. In `prepare_import_payload()`, carry `manual_ng_categories` and leave a missing key distinguishable from an explicit empty list.
5. In `validate()`, first reject a missing `"manual_ng_categories"` key, then call `normalize_manual_ng_categories(data["manual_ng_categories"])` and append its user-facing error.
6. In `_config_row_values()`, encode the normalized list into JSON.
7. Add `load_manual_ng_categories(config_id)` that selects only the required field and uses strict decode.

Do not use the existing forgiving `_parse_json()` for this feature.
Do not default a missing field to `[]` in ordinary save/import service calls. `create_config()` is the only path that supplies an explicit empty list automatically. This prevents stale callers from silently erasing a configured list.

- [ ] **Step 8: Run focused service verification**

Run:

```powershell
pytest tests/test_manual_ng_categories.py tests/test_config_service.py -q
```

Expected: all tests PASS.

- [ ] **Step 9: Commit Task 2**

```powershell
git add app/service/manual_ng_categories.py app/service/config_service.py tests/test_manual_ng_categories.py tests/test_config_service.py tests/test_config_package_service.py
git commit -m "feat: persist manual NG category configuration"
```

### Task 3: Add the Ordered Category Editor to the Config Page

**Files:**
- Create: `ui/widgets/manual_ng_category_list_editor.py`
- Create: `tests/test_manual_ng_category_list_editor.py`
- Modify: `ui/modules/config.py:1-835, 2775-2965`
- Modify: `tests/test_config_module.py:1-end`

- [ ] **Step 1: Write failing widget tests**

Create `tests/test_manual_ng_category_list_editor.py` with offscreen Qt coverage for:

- `set_categories()` followed by `categories()` preserves order;
- add appends one valid category;
- rename updates only the selected row;
- delete removes only the selected row;
- move up/down updates both order and selection;
- blank, duplicate-after-trim, newline, tab, and control-character names show a warning and do not mutate the list;
- every successful user mutation emits `categories_changed` once;
- programmatic `set_categories()` emits no dirty signal.

Prefer testing small private command methods that accept a supplied name, then separately test the `QInputDialog.getText` wiring. This avoids brittle tests that automate native dialog typing.

- [ ] **Step 2: Run widget tests and confirm RED**

Run:

```powershell
pytest tests/test_manual_ng_category_list_editor.py -q
```

Expected: import/collection FAIL because the editor does not exist.

- [ ] **Step 3: Implement the list editor**

Use `QListWidget` and five buttons. Give stable object names:

```python
self.category_list.setObjectName("manual_ng_category_list")
self.btn_add.setObjectName("manual_ng_category_add")
self.btn_rename.setObjectName("manual_ng_category_rename")
self.btn_delete.setObjectName("manual_ng_category_delete")
self.btn_move_up.setObjectName("manual_ng_category_move_up")
self.btn_move_down.setObjectName("manual_ng_category_move_down")
```

Expose:

```python
categories_changed = pyqtSignal()

def set_categories(self, categories: list[str]) -> None: ...
def categories(self) -> list[str]: ...
```

Reuse `normalize_manual_ng_categories()` for whole-list validation and a single-name helper from the same domain module for candidate validation. Display `QMessageBox.warning(self, "手动 NG 类别", message)` for invalid user edits.

- [ ] **Step 4: Run widget tests and confirm GREEN**

Run:

```powershell
pytest tests/test_manual_ng_category_list_editor.py -q
```

Expected: all widget tests PASS.

- [ ] **Step 5: Write failing ConfigModule integration tests**

Update `_detail_payload()` in `tests/test_config_module.py` to include `manual_ng_categories=[]` by default. Add tests proving:

- a top-level launcher card titled `手动 NG 类别` exists;
- `_populate_detail()` loads the list;
- `_collect_data()` returns the current order;
- loading a second config replaces rather than appends the first config's list;
- a list mutation makes `_has_unsaved_changes()` true;
- discard/reload restores the authoritative saved list;
- a save attempt that fails after a local category edit restores the authoritative persisted list through the existing failed-save recovery path and does not leave the edited list marked clean;
- new configs start empty;
- save passes the list into `ConfigService`.

- [ ] **Step 6: Run ConfigModule tests and confirm RED**

Run:

```powershell
pytest tests/test_config_module.py -q
```

Expected: the new integration tests FAIL because the card and collect/populate wiring are missing.

- [ ] **Step 7: Register and wire the top-level card**

Import `ManualNgCategoryListEditor`, instantiate it in `_init_ui()`, and register it with `_register_top_level_config_card()`:

```python
self.manual_ng_category_editor = ManualNgCategoryListEditor()
self._register_top_level_config_card(
    launcher_layout=top_launcher_layout,
    launcher_index=top_launcher_index,
    section_id="manual_ng_categories",
    title="手动 NG 类别",
    content=self.manual_ng_category_editor,
    default_position_anchor=top_launcher_panel,
    button_object_name="open_manual_ng_categories_card",
    dialog_object_name="top_level_manual_ng_categories_card_dialog",
)
top_launcher_index += 1
```

Add to `_collect_data()`:

```python
"manual_ng_categories": self.manual_ng_category_editor.categories(),
```

Add to `_populate_detail()` before marking the snapshot clean:

```python
self.manual_ng_category_editor.set_categories(
    data.get("manual_ng_categories", [])
)
```

The existing `_capture_editor_snapshot()` compares `_collect_data()`, so do not add a second dirty-state system.

Add a focused `_restore_authoritative_manual_ng_categories()` helper. On any uncommitted save/validation failure, after rollback/reconciliation, reload only `manual_ng_categories` for `_current_config_id` from `ConfigService` and call `set_categories()`; do not mark the entire editor clean or discard unrelated local edits. On a post-commit failure, the existing full authoritative reload remains the source of truth. If the focused reload itself fails, leave the local list dirty and include/log that recovery failure rather than pretending the list is authoritative.

- [ ] **Step 8: Run focused config UI verification**

Run:

```powershell
pytest tests/test_manual_ng_category_list_editor.py tests/test_config_module.py -q
```

Expected: all tests PASS.

- [ ] **Step 9: Commit Task 3**

```powershell
git add ui/widgets/manual_ng_category_list_editor.py ui/modules/config.py tests/test_manual_ng_category_list_editor.py tests/test_config_module.py
git commit -m "feat: add manual NG category config editor"
```

### Task 4: Version Configuration Packages and Enforce the New Field

**Files:**
- Modify: `app/service/config_package_service.py:30-37, 882-1054`
- Modify: `tests/test_config_package_service.py:350-end`

- [ ] **Step 1: Write failing package-v2 tests**

Update package test helpers so valid packages use:

```python
"format_version": 2
```

and every valid `config` payload contains:

```python
"manual_ng_categories": ["异响", "漏装"]
```

Add tests that prove:

- export writes `format_version == 2`;
- export includes the normalized ordered category list;
- v2 inspect/import restores the list;
- v1 is rejected with `format_version` before any DB/file mutation;
- an unknown version is rejected;
- v2 missing `config.manual_ng_categories` is rejected;
- v2 with invalid category data is rejected, including an isolated surrogate encoded in JSON as `"\ud800"`;
- all rejection tests compare config rows and managed asset directories before/after to prove no side effects.

- [ ] **Step 2: Run package tests and confirm RED**

Run:

```powershell
pytest tests/test_config_package_service.py -q
```

Expected: FAIL because production still exports/expects v1 and does not require the field.

- [ ] **Step 3: Implement package format v2**

Change:

```python
FORMAT_VERSION = 2
```

Update `_validate_layout()` error text from “Version 1” to “Version 2”.

In `_validate_import_config_payload()`, before asset processing:

```python
if "manual_ng_categories" not in config_root:
    raise ValueError(
        "config/config.json config.manual_ng_categories is missing"
    )
try:
    normalize_manual_ng_categories(config_root["manual_ng_categories"])
except ValueError as exc:
    raise ValueError(
        f"config/config.json config.manual_ng_categories is invalid: {exc}"
    ) from exc
```

Keep manifest validation before config payload validation so v1 fails deterministically on version. Keep all validation inside archive inspection, before extraction and persistence.

- [ ] **Step 4: Run focused package and config-service tests**

Run:

```powershell
pytest tests/test_config_package_service.py tests/test_config_service.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit Task 4**

```powershell
git add app/service/config_package_service.py tests/test_config_package_service.py
git commit -m "feat: require manual NG categories in config packages"
```

### Task 5: Persist Category Fields in One TestSession Row Update

**Files:**
- Modify: `app/service/analysis/test_session.py:41-489`
- Modify: `tests/test_test_session.py:1520-1855`

- [ ] **Step 1: Write failing TestSession persistence tests**

Extend existing automatic/manual persistence tests:

```python
assert rec.manual_ng_category is None  # automatic OK/NG
```

Add:

```python
def test_apply_manual_label_persists_manual_ng_category(...):
    result = session.apply_manual_label(
        "NG",
        manual_ng_category="异响",
    )
    rec = AudioDataTable.get_by_id(session._audio_data_id)
    assert (rec.labels, rec.label_source, rec.manual_ng_category) == (
        "NG",
        "manual",
        "异响",
    )


def test_apply_manual_ok_clears_existing_manual_ng_category(...):
    ...
    result = session.apply_manual_label("OK")
    assert AudioDataTable.get_by_id(...).manual_ng_category is None
```

Also test:

- NG with `None`;
- OK with a non-empty category rejected;
- invalid/control-character category rejected before trimming;
- missing row and update exception;
- failure never partially changes the three fields;
- `_audio_config_metadata()` / saved `config_content` contains the normalized `manual_ng_categories` list from the run config.

- [ ] **Step 2: Run TestSession tests and confirm RED**

Run:

```powershell
pytest tests/test_test_session.py -q
```

Expected: new assertions/tests FAIL because the field is not written or cleared.

- [ ] **Step 3: Implement TestSession's one-row update**

In `_persist_audio_result_metadata()`, whenever an automatic label is written, include:

```python
update_payload["manual_ng_category"] = None
```

Change `apply_manual_label()` to build one update payload:

```python
update_payload = {
    "labels": normalized,
    "label_source": "manual",
    "manual_ng_category": (
        validated_category if normalized == "NG" else None
    ),
}
```

Execute exactly one `AudioDataTable.update(**update_payload)` query. Do not update category in a separate statement. Session-level category validation is defensive; for a non-empty category it must reject original control/line-break characters before any trim and must reject category data on OK.

- [ ] **Step 4: Run TestSession tests and confirm GREEN**

Run:

```powershell
pytest tests/test_test_session.py -q
```

Expected: all tests PASS, including the explicit config snapshot assertion.

- [ ] **Step 5: Commit Task 5**

```powershell
git add app/service/analysis/test_session.py tests/test_test_session.py
git commit -m "feat: persist manual NG category on test records"
```

### Task 6: Enforce Category Context in One SQLite Write-Intent Transaction

**Files:**
- Modify: `app/service/analysis/test_manager.py:558-625, 2797-2835`
- Modify: `tests/test_test_manager/test_manual_judgment.py:1-end`

- [ ] **Step 1: Write failing unit-level manager context tests**

Update `_make_manager()` so `services.config_service.load_manual_ng_categories()` returns a controlled persisted list. Update NG calls to use `build_manual_ng_selection_context()`.

Add tests for:

- exact `config_id + run_id` completed-session lookup;
- configured list + selected valid category reaches `session.apply_manual_label("NG", manual_ng_category="异响")`;
- selected category must match exactly: `" 异响 "` is rejected rather than trimmed to `"异响"`;
- empty list accepts only `None`;
- OK rejects a supplied category;
- context `config_id` or `run_id` mismatch rejects;
- current list rename, delete, add unrelated item, or reorder rejects via fingerprint even if selected text still exists;
- config row deletion and malformed JSON become failure results;
- every failure skips `session.apply_manual_label`;
- automatic/no-result and stopped-partial saved sessions still support manual NG through the new context.

- [ ] **Step 2: Add real file-backed transaction and rollback tests**

Use a temporary file `SqliteDatabase`, bind `TestConfigTable` and `AudioDataTable`, and create real rows. The test double for the completed session must update the real `AudioDataTable` row so assertions exercise SQLite, not `MagicMock`.

Add these proofs:

```python
def test_manual_ng_config_read_and_audio_update_share_transaction(...):
    # Assert db.in_transaction() from both the config-load probe
    # and the session apply probe.
    ...


def test_manual_ng_session_failure_rolls_back_all_three_fields(...):
    # Have the session update the real row and then return failure.
    # Manager must force rollback; labels/source/category stay original.
    ...


def test_manual_ng_write_intent_blocks_concurrent_config_change(...):
    # During session.apply_manual_label(), open a second sqlite3 connection
    # with timeout=0 and attempt to update manual_ng_categories.
    # It must fail with "database is locked" while the manager transaction
    # is open; then the audio update commits against the unchanged list.
    ...
```

Also retain a separate test where a second connection successfully changes the list before submission; the old context must then fail fingerprint validation and leave the audio row unchanged.

- [ ] **Step 3: Run manager tests and confirm RED**

Run:

```powershell
pytest tests/test_test_manager/test_manual_judgment.py -q
```

Expected: FAIL because the manager does not accept a context, use a write-intent transaction, or force rollback for failure results.

- [ ] **Step 4: Implement the authoritative transaction and rollback sentinel**

Introduce a private exception used only to force rollback while preserving the normal result:

```python
class _ManualJudgmentRollback(Exception):
    def __init__(self, result: ManualLabelUpdateResult):
        super().__init__(result.message)
        self.result = result
```

For NG, enter `TestConfigTable._meta.database.atomic("IMMEDIATE")` before any authoritative lookup. Inside that same transaction:

1. Re-check active/analyzing state.
2. Resolve `_completed_sessions[config_id]`.
3. Verify its exact `run_id`.
4. Verify context identity equals explicit `config_id + run_id`.
5. Call `self._services.config_service.load_manual_ng_categories(config_id)`.
6. Compare the complete ordered-list fingerprint and exact selected text.
7. Call `session.apply_manual_label("NG", manual_ng_category=validated_name)`.
8. If it returns `success=False`, raise `_ManualJudgmentRollback(result)` so the outer transaction rolls back.

Catch `_ManualJudgmentRollback` outside the `with` block and return its stored result. Catch missing config, category validation, and expected database errors into clear failure results only after SQLite has rolled back.

The `IMMEDIATE` lock is required: it acquires SQLite write intent before the configuration read so another connection cannot change the list in the read-to-update window.

For OK, retain the exact-session checks and one-row `TestSession` update. Reject any category/context category data. It may use the same rollback-sentinel pattern for consistency.

- [ ] **Step 5: Run manager and session tests**

Run:

```powershell
pytest tests/test_test_manager/test_manual_judgment.py tests/test_test_session.py -q
```

Expected: all tests PASS, including real file-backed transaction, rollback, and second-connection lock proofs.

- [ ] **Step 6: Commit Task 6**

```powershell
git add app/service/analysis/test_manager.py tests/test_test_manager/test_manual_judgment.py
git commit -m "feat: validate manual NG context transactionally"
```

### Task 7: Capture Controller Identity and Preserve Reset Semantics

**Files:**
- Modify: `app/service/factory_mode_controller.py:257-297, 780-787`
- Modify: `tests/test_factory_mode_controller/test_manual_judgment.py:1-end`
- Modify: `tests/test_factory_mode_controller/_support.py:130-190` if shared config fixtures need the new list

- [ ] **Step 1: Write failing controller context/identity tests**

Extend `_TestStub.apply_manual_judgment()` to record context and category. Give the config-service stub `load_manual_ng_categories()`.

Add tests:

- `prepare_manual_ng_judgment()` captures displayed `config_id`, stopped/review `run_id`, ordered categories, and correct fingerprint;
- missing display, missing run id, busy state, deleted config, and damaged categories return a failed preparation;
- direct NG with an empty persisted list may internally prepare and retains the existing immediate/reset behavior;
- direct NG with a non-empty persisted list and no caller-supplied context is rejected, even if a category string is supplied;
- configured manual NG with a supplied context requires a chosen category;
- changing displayed `config_id` or `run_id` after preparation rejects without backend call/reset;
- successful configured NG passes the exact captured context/category and emits only `manual_judgment_ng`;
- backend failure preserves the review page;
- manual OK path and `manual_judgment_ok` reset remain unchanged;
- no PLC event/result method is called by category selection or persistence.

- [ ] **Step 2: Run controller tests and confirm RED**

Run:

```powershell
pytest tests/test_factory_mode_controller/test_manual_judgment.py -q
```

Expected: FAIL because preparation/context-aware submission do not exist.

- [ ] **Step 3: Implement preparation and identity drift rejection**

Factor existing eligibility/identity lookup into a private helper used by preparation and apply. Preparation requires a non-empty run id:

```python
categories = self.services.config_service.load_manual_ng_categories(config_id)
context = build_manual_ng_selection_context(
    config_id=config_id,
    run_id=run_id,
    categories=categories,
)
return ManualNgSelectionPreparation(True, context=context)
```

Direct-call rule:

```python
if normalized == "NG" and selection_context is None:
    if manual_ng_category not in (None, ""):
        return ManualLabelUpdateResult(False, "请重新选择手动 NG 类别")
    preparation = self.prepare_manual_ng_judgment()
    if not preparation.success or preparation.context is None:
        return ManualLabelUpdateResult(False, preparation.message)
    if preparation.context.categories:
        return ManualLabelUpdateResult(False, "请选择手动 NG 类别")
    selection_context = preparation.context
```

This automatic preparation exists only for the empty-list behavior. A configured list must always arrive with the context captured before the dialog opened; the controller must never manufacture a fresh context to make a supplied category pass.

Immediately before delegating:

```python
if (
    self._state.displayed_config_id != selection_context.config_id
    or self._manual_judgment_run_id(selection_context.config_id)
    != selection_context.run_id
):
    return ManualLabelUpdateResult(False, "当前测试结果已变化，请重新选择类别")
```

Only call `clear_for_next_run()` after backend success, using the existing reason strings.

- [ ] **Step 4: Run controller plus manager regression tests**

Run:

```powershell
pytest tests/test_factory_mode_controller/test_manual_judgment.py tests/test_test_manager/test_manual_judgment.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit Task 7**

```powershell
git add app/service/factory_mode_controller.py tests/test_factory_mode_controller/test_manual_judgment.py tests/test_factory_mode_controller/_support.py
git commit -m "feat: bind manual NG selection to displayed run"
```

### Task 8: Add the Required-Selection Dialog and Control-Panel Flow

**Files:**
- Create: `ui/popups/manual_ng_category_dialog.py`
- Create: `tests/test_manual_ng_category_dialog.py`
- Modify: `ui/modules/control.py:1-205`
- Modify: `tests/test_control_module.py:1-500`

- [ ] **Step 1: Write failing dialog tests**

Create `tests/test_manual_ng_category_dialog.py` covering:

- categories display in supplied order;
- no item is selected initially;
- confirm is disabled initially;
- selecting one row enables confirm;
- `selected_category()` returns only the active row;
- double-click does not bypass a missing selection;
- cancel/close yields `Rejected`.

- [ ] **Step 2: Run dialog tests and confirm RED**

Run:

```powershell
pytest tests/test_manual_ng_category_dialog.py -q
```

Expected: import/collection FAIL because the dialog does not exist.

- [ ] **Step 3: Implement the modal single-choice dialog**

Use `QListWidget` with `SingleSelection` and `QDialogButtonBox(Ok | Cancel)`. Do not preselect row zero.

```python
class ManualNgCategoryDialog(QDialog):
    def __init__(self, categories: tuple[str, ...], parent=None):
        ...
        self.setWindowTitle("选择手动 NG 类别")
        self.category_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._ok_button.setEnabled(False)
        self.category_list.itemSelectionChanged.connect(
            self._sync_confirm_enabled
        )

    def selected_category(self) -> str | None:
        item = self.category_list.currentItem()
        return item.text() if item is not None else None
```

- [ ] **Step 4: Run dialog tests and confirm GREEN**

Run:

```powershell
pytest tests/test_manual_ng_category_dialog.py -q
```

Expected: all dialog tests PASS.

- [ ] **Step 5: Write failing ControlModule tests**

Extend the factory-mode stub with:

- `prepare_manual_ng_judgment`;
- context-aware `apply_manual_judgment`.

Add tests:

- manual OK still calls apply directly and never prepares/opens a dialog;
- NG preparation failure shows one warning and does not apply;
- empty categories call apply immediately with captured context and `None`;
- configured categories open the dialog;
- accepted selection calls apply with the exact context/category;
- cancel and window close do not call apply, log, reset, or warn;
- stale-context/backend failure after dialog confirmation shows warning and does not emit success log;
- successful NG emits the existing `Manual judgment: FAIL` log exactly once.

- [ ] **Step 6: Run control tests and confirm RED**

Run:

```powershell
pytest tests/test_control_module.py -q
```

Expected: new tests FAIL because NG still applies immediately.

- [ ] **Step 7: Implement the NG-only preflight flow**

Keep the OK branch structurally unchanged. For NG:

```python
preparation = factory_mode.prepare_manual_ng_judgment()
if not preparation.success or preparation.context is None:
    self._show_manual_judgment_warning(preparation.message)
    return

context = preparation.context
selected = None
if context.categories:
    dialog = ManualNgCategoryDialog(context.categories, parent=self)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    selected = dialog.selected_category()
    if selected is None:
        return

result = factory_mode.apply_manual_judgment(
    "NG",
    selection_context=context,
    manual_ng_category=selected,
)
```

Do not add timers, defaults, “无类别” choices, or manual input.

- [ ] **Step 8: Run the complete UI/manual slice**

Run:

```powershell
pytest tests/test_manual_ng_category_dialog.py tests/test_control_module.py tests/test_factory_mode_controller/test_manual_judgment.py -q
```

Expected: all tests PASS.

- [ ] **Step 9: Commit Task 8**

```powershell
git add ui/popups/manual_ng_category_dialog.py ui/modules/control.py tests/test_manual_ng_category_dialog.py tests/test_control_module.py
git commit -m "feat: require configured category for manual NG"
```

### Task 9: Display and Export the Persisted Category

**Files:**
- Modify: `app/service/audio_data_service.py:30-250`
- Modify: `ui/modules/data_management.py:308-570`
- Modify: `tests/test_audio_data_service.py:1-end`
- Modify: `tests/test_data_management_module.py:1-180`

- [ ] **Step 1: Write failing service/export tests**

Extend service column tests:

```python
assert "manual_ng_category" in columns
assert columns.index("manual_ng_category") > columns.index("label_source")
```

Create an NG record with `manual_ng_category="异响"` and assert the exported SQLite database has the column and exact value. Also cover a `NULL` category.

- [ ] **Step 2: Write failing data-management UI tests**

Add `manual_ng_category` to the stub's column list and records. Assert:

- the header is `手动NG类别`;
- a selected category renders unchanged;
- `None` renders blank;
- no new category filter control appears;
- existing label-source mappings remain unchanged.

- [ ] **Step 3: Run focused tests and confirm RED**

Run:

```powershell
pytest tests/test_audio_data_service.py tests/test_data_management_module.py -q
```

Expected: FAIL because the export model and header mapping omit the field.

- [ ] **Step 4: Implement export and display support**

In `_build_export_model()`:

```python
manual_ng_category = AudioDataTable.manual_ng_category.clone()
```

In `DataManagementModule`:

```python
table_header_labels["manual_ng_category"] = "手动NG类别"
header_min_widths["manual_ng_category"] = 120
```

No special display mapping is needed beyond existing `None -> ""`; do not add a filter.

- [ ] **Step 5: Run data-management verification**

Run:

```powershell
pytest tests/test_audio_data_service.py tests/test_data_management_module.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: Run cross-feature regression tests**

Run:

```powershell
pytest tests/test_workorder_module.py tests/test_video_module.py tests/test_factory_mode_controller/test_manual_judgment.py tests/test_control_module.py -q
```

Expected: all tests PASS, proving existing SN reset, video preservation, controller reset reasons, and control behavior remain intact.

- [ ] **Step 7: Commit Task 9**

```powershell
git add app/service/audio_data_service.py ui/modules/data_management.py tests/test_audio_data_service.py tests/test_data_management_module.py
git commit -m "feat: show and export manual NG categories"
```

### Task 10: Prepare the External Database Artifact and Run Final Verification

**Files:**
- Runtime artifact only, not tracked: main checkout `data/jingchen.db`
- No source files should change in this task.

This task creates a recoverable backup and updates the external prepared database once. It does not add a migration script to the repository and the application must never run these statements at startup.

- [ ] **Step 1: Confirm the source tree is clean except known user-owned changes**

Run:

```powershell
git status --short
git log --oneline -10
```

Expected:

- the nine source feature-task commits are present;
- no feature file is left uncommitted;
- pre-existing user-owned `base/utils.py`, `.agents/`, and `.codex/` changes remain untouched.

- [ ] **Step 2: Resolve and validate the exact external DB target**

Run:

```powershell
$commonGitDir = (git rev-parse --path-format=absolute --git-common-dir).Trim()
$mainCheckout = Split-Path -Parent $commonGitDir
$preparedDb = Join-Path $mainCheckout 'data\jingchen.db'
$resolvedDb = (Resolve-Path -LiteralPath $preparedDb).Path
$resolvedDataDir = (Resolve-Path -LiteralPath (Join-Path $mainCheckout 'data')).Path
if ((Split-Path -Parent $resolvedDb) -ne $resolvedDataDir) {
    throw "Prepared DB escaped the intended data directory: $resolvedDb"
}
Write-Output $resolvedDb
```

Expected: one absolute path ending in `JingChengMechanical\data\jingchen.db`.

- [ ] **Step 3: Stop the application and preflight table/column definitions**

The desktop application must be stopped before backup or schema preparation. Do not terminate a user process automatically. If the implementer cannot confirm the application is stopped, report the deployment-artifact step as blocked and ask the orchestrator to coordinate shutdown.

Run:

```powershell
$env:MANUAL_NG_PREPARED_DB = $resolvedDb
@'
import os
import sqlite3
from pathlib import Path

path = Path(os.environ["MANUAL_NG_PREPARED_DB"]).resolve()
con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
try:
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"source database integrity_check failed: {integrity}")

    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    required_tables = {"test_config_table", "audio_data_table"}
    if not required_tables.issubset(tables):
        raise RuntimeError(
            f"prepared database missing tables: {required_tables - tables}"
        )

    expected = {
        ("test_config_table", "manual_ng_categories"):
            ("TEXT", True, "'[]'", False),
        ("audio_data_table", "manual_ng_category"):
            ("TEXT", False, None, False),
    }
    incompatible = []
    for (table, column), expected_definition in expected.items():
        rows = {
            row[1]: (row[2].upper(), bool(row[3]), row[4], bool(row[5]))
            for row in con.execute(f"PRAGMA table_info({table})")
        }
        actual = rows.get(column)
        print(table, column, actual or "MISSING")
        if actual is not None and actual != expected_definition:
            incompatible.append(
                f"{table}.{column}: expected={expected_definition}, actual={actual}"
            )
    if incompatible:
        raise RuntimeError(
            "incompatible existing manual-NG columns; no ALTER is allowed:\n"
            + "\n".join(incompatible)
        )
finally:
    con.close()
'@ | python -
```

Expected: source integrity is `ok`; both tables exist; each new column is either absent or exactly matches the declared type, nullability, default, and primary-key status. Any incompatible existing definition stops the task before backup or ALTER.

- [ ] **Step 4: Create and verify a recoverable SQLite backup**

Run:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDb = "$resolvedDb.before-manual-ng-categories-$stamp.bak"
$env:MANUAL_NG_PREPARED_BACKUP = $backupDb
@'
import os
import sqlite3
from pathlib import Path

source_path = Path(os.environ["MANUAL_NG_PREPARED_DB"]).resolve()
backup_path = Path(os.environ["MANUAL_NG_PREPARED_BACKUP"]).resolve()
if backup_path.parent != source_path.parent:
    raise RuntimeError("backup escaped the prepared database directory")
if backup_path.exists():
    raise RuntimeError(f"backup target already exists: {backup_path}")

source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
backup = sqlite3.connect(str(backup_path))
try:
    source.backup(backup)
    backup.commit()
    integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"backup integrity_check failed: {integrity}")
    print(f"verified SQLite backup: {backup_path}")
finally:
    backup.close()
    source.close()
'@ | python -
```

Expected: SQLite backup API completes, backup `integrity_check` is `ok`, and the verified backup path is printed. Preserve this path for final handoff. A raw `Copy-Item` of the SQLite main file is forbidden because it can miss WAL state.

- [ ] **Step 5: Build the new prepared schema once**

Run this one-time artifact-preparation command; do not save it as a repository script:

```powershell
@'
import os
import sqlite3
from pathlib import Path

path = Path(os.environ["MANUAL_NG_PREPARED_DB"]).resolve()
con = sqlite3.connect(str(path), timeout=0)
try:
    expected = {
        ("test_config_table", "manual_ng_categories"):
            ("TEXT", True, "'[]'", False),
        ("audio_data_table", "manual_ng_category"):
            ("TEXT", False, None, False),
    }
    current = {}
    incompatible = []
    for (table, column), expected_definition in expected.items():
        rows = {
            row[1]: (row[2].upper(), bool(row[3]), row[4], bool(row[5]))
            for row in con.execute(f"PRAGMA table_info({table})")
        }
        current[(table, column)] = rows.get(column)
        if rows.get(column) is not None and rows[column] != expected_definition:
            incompatible.append(
                f"{table}.{column}: expected={expected_definition}, "
                f"actual={rows[column]}"
            )
    if incompatible:
        raise RuntimeError(
            "incompatible existing columns; refusing all ALTER statements:\n"
            + "\n".join(incompatible)
        )

    con.execute("BEGIN IMMEDIATE")
    if current[("test_config_table", "manual_ng_categories")] is None:
        con.execute(
            "ALTER TABLE test_config_table "
            "ADD COLUMN manual_ng_categories TEXT NOT NULL DEFAULT '[]'"
        )
    if current[("audio_data_table", "manual_ng_category")] is None:
        con.execute(
            "ALTER TABLE audio_data_table "
            "ADD COLUMN manual_ng_category TEXT"
        )

    for (table, column), expected_definition in expected.items():
        rows = {
            row[1]: (row[2].upper(), bool(row[3]), row[4], bool(row[5]))
            for row in con.execute(f"PRAGMA table_info({table})")
        }
        actual = rows.get(column)
        if actual != expected_definition:
            raise RuntimeError(
                f"post-update definition mismatch for {table}.{column}: "
                f"expected={expected_definition}, actual={actual}"
            )
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"post-update integrity_check failed: {integrity}")
    con.commit()
    print("prepared database schema definitions and integrity: PASS")
except Exception:
    if con.in_transaction:
        con.rollback()
    raise
finally:
    con.close()
'@ | python -
```

Expected: exit 0 with exact definitions and `integrity_check` verified before the transaction commits. Any definition/integrity failure rolls back both ALTER statements. Re-running is safe when both columns already match. If commit itself or later startup verification fails, stop and report the verified backup path; do not overwrite the prepared DB from backup without explicit caller authorization.

- [ ] **Step 6: Verify the real startup gate against the prepared DB**

Run in a fresh Python process so `APP_DB_PATH` is read before model import:

```powershell
$env:APP_DB_PATH = $resolvedDb
python -c "from app.service.db_connection_service import DbConnectionService; service = DbConnectionService(); service.connect(); print('prepared database startup gate: PASS'); service.close()"
Remove-Item Env:APP_DB_PATH
```

Expected: `prepared database startup gate: PASS`.

- [ ] **Step 7: Run all focused feature tests**

Run:

```powershell
pytest tests/test_manual_ng_categories.py tests/test_db_connection_service.py tests/test_config_service.py tests/test_manual_ng_category_list_editor.py tests/test_config_module.py tests/test_config_package_service.py tests/test_test_session.py tests/test_test_manager/test_manual_judgment.py tests/test_factory_mode_controller/test_manual_judgment.py tests/test_manual_ng_category_dialog.py tests/test_control_module.py tests/test_audio_data_service.py tests/test_data_management_module.py tests/test_workorder_module.py tests/test_video_module.py -q
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 8: Run the complete test suite**

Run:

```powershell
pytest -q
```

Expected: zero failures. If unrelated known failures exist, stop and report exact test names and evidence; do not claim completion or weaken/remove tests.

- [ ] **Step 9: Verify commit scope and external artifact status**

Run:

```powershell
git status --short
git log --oneline --decorate -10
```

Expected:

- no uncommitted feature source files;
- only pre-existing user-owned changes remain;
- `data/jingchen.db` and its backup remain ignored/untracked as external deployment artifacts;
- task commits are present in order.

Do not commit `data/jingchen.db` or the backup. Report the resolved prepared DB path, backup path, startup-gate output, focused test count, and full-suite result to the orchestrator.

## Final Review Checklist

Before reporting implementation complete, reviewers must verify:

- [ ] Automatic judgment still persists immediately without waiting for manual input.
- [ ] Automatic OK/NG and manual OK always clear `manual_ng_category`.
- [ ] Saved `config_content` contains the normalized `manual_ng_categories` run snapshot.
- [ ] No configured categories means manual NG has no extra visible step.
- [ ] Configured categories require an active single selection.
- [ ] Original submitted category text must match exactly; trimming cannot turn an invalid submission into a match.
- [ ] Cancel/close has no DB, state, reset, SN, video, or log side effect.
- [ ] Config-editor save failure restores the authoritative persisted category list.
- [ ] `config_id + run_id` drift is rejected.
- [ ] Any ordered-list content/order drift is rejected by fingerprint.
- [ ] Exact session resolution, config re-read, fingerprint comparison, and audio-row update share one SQLite `IMMEDIATE` transaction.
- [ ] A failure result forces transaction rollback; a second writer cannot alter the list in the read-to-update window.
- [ ] Manual NG updates one existing audio row and never inserts a second row.
- [ ] Config package v2 is required; v1 and missing-field packages fail before side effects.
- [ ] Both file-database columns are startup requirements with no runtime repair.
- [ ] Data management and exported SQLite data contain the selected category.
- [ ] PLC behavior and payloads remain OK/NG-only.
- [ ] The stopped-app external prepared DB has a SQLite-API backup with `integrity_check=ok`, exact column definitions, and a passing real startup gate.
- [ ] Focused and full test commands have fresh passing evidence.
